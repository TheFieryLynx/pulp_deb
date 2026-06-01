import logging
import posixpath
from gettext import gettext as _

from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import models

from pulpcore.plugin.models import (
    AutoAddObjPermsMixin,
    BaseModel,
    Content,
    ContentArtifact,
    Repository,
)
from pulpcore.plugin.repo_version_utils import (
    validate_duplicate_content,
    validate_version_paths,
)
from pulpcore.plugin.util import batch_qs, get_domain_pk

from pulp_deb.app.models import (
    AptReleaseSigningService,
    AptRemote,
    GenericContent,
    InstallerFileIndex,
    InstallerPackage,
    Package,
    PackageIndex,
    PackageReleaseComponent,
    Release,
    ReleaseArchitecture,
    ReleaseComponent,
    ReleaseFile,
    SourceIndex,
    SourcePackage,
    SourcePackageReleaseComponent,
)

log = logging.getLogger(__name__)


class AptRepository(Repository, AutoAddObjPermsMixin):
    """
    A Repository for DebContent.
    """

    TYPE = "deb"
    CONTENT_TYPES = [
        GenericContent,
        InstallerFileIndex,
        InstallerPackage,
        Package,
        PackageIndex,
        PackageReleaseComponent,
        Release,
        ReleaseArchitecture,
        ReleaseComponent,
        ReleaseFile,
        SourceIndex,
        SourcePackage,
        SourcePackageReleaseComponent,
    ]
    REMOTE_TYPES = [
        AptRemote,
    ]

    publish_upstream_release_fields = models.BooleanField(default=True)

    signing_service = models.ForeignKey(
        AptReleaseSigningService, on_delete=models.PROTECT, null=True
    )
    # Implicit signing_service_release_overrides

    autopublish = models.BooleanField(default=False)

    def on_new_version(self, version):
        """
        Called when new repository versions are created.

        Args:
            version: The new repository version.
        """
        super().on_new_version(version)

        # avoid circular import issues
        from pulp_deb.app import tasks

        if self.autopublish:
            tasks.publish(
                repository_version_pk=version.pk,
                # We currently support only automatically creating a structured
                # publication
                simple=False,
                structured=True,
                signing_service_pk=getattr(self.signing_service, "pk", None),
            )

    class Meta:
        default_related_name = "%(app_label)s_%(model_name)s"
        permissions = [
            ("manage_roles_aptrepository", "Can manage roles on APT repositories"),
            ("modify_content_aptrepository", "Add content to, or remove content from a repository"),
            ("repair_aptrepository", "Copy an APT repository"),
            ("sync_aptrepository", "Sync an APT repository"),
            ("delete_aptrepository_version", "Delete a repository version"),
        ]

    def release_signing_service(self, release):
        """
        Return the Signing Service specified in the overrides if there is one for this release,
        else return self.signing_service.
        """
        if isinstance(release, Release):
            release = release.distribution
        try:
            override = self.signing_service_release_overrides.get(release_distribution=release)
            return override.signing_service
        except AptRepositoryReleaseServiceOverride.DoesNotExist:
            return self.signing_service

    def initialize_new_version(self, new_version):
        """
        Remove old metadata from the repo before performing anything else for the new version. This
        way, we ensure any syncs will re-add all metadata relevant for the latest sync, but old
        metadata (which may no longer be appropriate for the new RepositoryVersion is never
        retained.
        """
        new_version.remove_content(ReleaseFile.objects.filter(pulp_domain=get_domain_pk()))
        new_version.remove_content(PackageIndex.objects.filter(pulp_domain=get_domain_pk()))
        new_version.remove_content(InstallerFileIndex.objects.filter(pulp_domain=get_domain_pk()))

    def finalize_new_version(self, new_version):
        """
        Finalize and validate the new repository version.

        Ensure there are no duplication of added package in debian repository.

        Args:
            new_version (pulpcore.app.models.RepositoryVersion): The incomplete RepositoryVersion to
                finalize.

        """
        handle_duplicate_packages(new_version)
        handle_duplicate_source_packages(new_version)
        handle_duplicate_releases(new_version)
        validate_duplicate_content(new_version)
        validate_version_paths(new_version)


class AptRepositoryReleaseServiceOverride(BaseModel):
    """
    Override the SigningService that a single Release will use in this AptRepository.
    """

    repository = models.ForeignKey(
        AptRepository, on_delete=models.CASCADE, related_name="signing_service_release_overrides"
    )
    signing_service = models.ForeignKey(AptReleaseSigningService, on_delete=models.PROTECT)
    release_distribution = models.TextField()

    class Meta:
        unique_together = (("repository", "release_distribution"),)


def find_dist_components(package_ids, content_set):
    """
    Given a list of package_ids and a content_set, this function will find all distribution-
    component combinations that exist for the given package_ids within the given content_set.

    Returns a set of strings, e.g.: "buster main".
    """
    # PackageReleaseComponents:
    package_prc_qs = PackageReleaseComponent.objects.filter(package__in=package_ids).only("pk")
    prc_content_qs = content_set.filter(pk__in=package_prc_qs)
    prc_qs = PackageReleaseComponent.objects.filter(pk__in=prc_content_qs.only("pk"))

    # ReleaseComponents:
    distribution_components = set()
    for prc in prc_qs.select_related("release_component").iterator():
        distribution = prc.release_component.distribution
        component = prc.release_component.component
        distribution_components.add(distribution + " " + component)

    return distribution_components


def handle_duplicate_packages(new_version):
    """
    pulpcore's remove_duplicates does not work for .deb packages, since identical duplicate
    packages (same sha256) are rare, but allowed, while duplicates with different sha256 are
    forbidden. As such we need our own version of this function for .deb packages. Since we are
    already building our own function, we will also be combining the functionality of pulpcore's
    remove_duplicates and validate_duplicate_content within this function.
    """
    content_qs_added = new_version.added(base_version=new_version.base_version)
    if new_version.base_version:
        content_qs_existing = new_version.base_version.content
    else:
        try:
            content_qs_existing = new_version.previous().content
        except new_version.DoesNotExist:
            content_qs_existing = Content.objects.none()
    package_types = {
        Package.get_pulp_type(): Package,
        InstallerPackage.get_pulp_type(): InstallerPackage,
    }
    repo_key_fields = ("package", "version", "architecture")

    for pulp_type, package_obj in package_types.items():
        # First handle duplicates within the packages added to new_version
        package_qs_added = package_obj.objects.filter(
            pk__in=content_qs_added.filter(pulp_type=pulp_type)
        )
        added_unique = package_qs_added.distinct(*repo_key_fields)
        added_checksum_unique = package_qs_added.distinct(*repo_key_fields, "sha256")

        if added_unique.count() < added_checksum_unique.count():
            if log.isEnabledFor(logging.DEBUG):
                message = _(
                    'New repository version is trying to add different versions, of package "{}", '
                    'to each of the following distribution-component combinations "{}"!'
                )
                package_qs_added_dups = added_checksum_unique.difference(added_unique)
                for package_fields in package_qs_added_dups.values(*repo_key_fields, "sha256"):
                    package_fields.pop("sha256")
                    duplicate_package_ids = package_qs_added.filter(**package_fields).only("pk")
                    distribution_components = find_dist_components(
                        duplicate_package_ids, content_qs_added
                    )
                    log.debug(message.format(package_fields, distribution_components))

            message = _(
                "Cannot create repository version since there are newly added packages with the "
                "same name, version, and architecture, but a different checksum. If the log level "
                "is DEBUG, you can find a list of affected packages in the Pulp log. You can often "
                "work around this issue by restricting syncs to only those distirbution component "
                "combinations, that do not contain colliding duplicates!"
            )
            raise ValueError(message)

        # Now remove existing packages that are duplicates of any packages added to new_version
        if package_qs_added.count() and content_qs_existing.count():
            for batch in batch_qs(package_qs_added.values(*repo_key_fields, "sha256")):
                find_dup_qs = models.Q()

                for content_dict in batch:
                    sha256 = content_dict.pop("sha256")
                    item_query = models.Q(**content_dict) & ~models.Q(sha256=sha256)
                    find_dup_qs |= item_query

                package_qs_duplicates = (
                    package_obj.objects.filter(pk__in=content_qs_existing)
                    .filter(find_dup_qs)
                    .only("pk")
                )
                prc_qs_duplicates = (
                    PackageReleaseComponent.objects.filter(pk__in=content_qs_existing)
                    .filter(package__in=package_qs_duplicates)
                    .only("pk")
                )
                if package_qs_duplicates.count():
                    message = _("Removing duplicates for type {} from new repo version.")
                    log.warning(message.format(pulp_type))
                    new_version.remove_content(package_qs_duplicates)
                    new_version.remove_content(prc_qs_duplicates)


def _get_existing_content(new_version):
    if new_version.base_version:
        return new_version.base_version.content
    try:
        return new_version.previous().content
    except new_version.DoesNotExist:
        return Content.objects.none()


def _source_package_sha256(source_package):
    try:
        return source_package.sha256
    except (AttributeError, MultipleObjectsReturned, ObjectDoesNotExist):
        return None


def _source_package_artifact_set(source_package):
    artifact_set = []
    rows = (
        source_package.contentartifact_set.select_related("artifact")
        .order_by("relative_path", "artifact__sha256", "artifact__size")
        .values_list("relative_path", "artifact__sha256", "artifact__size")
    )
    for relative_path, sha256, size in rows:
        if not relative_path or sha256 is None or size is None:
            return None
        artifact_set.append((posixpath.basename(relative_path), sha256, size))

    if not artifact_set:
        return None
    return tuple(sorted(artifact_set))


def _source_package_dsc_content_artifact(source_package):
    dsc_filename = source_package.derived_dsc_filename()
    dsc_content_artifacts = [
        content_artifact
        for content_artifact in source_package.contentartifact_set.select_related("artifact")
        if posixpath.basename(content_artifact.relative_path) == dsc_filename
    ]
    if len(dsc_content_artifacts) == 1:
        return dsc_content_artifacts[0]
    current_dir = posixpath.dirname(source_package.relative_path)
    current_dir_matches = [
        content_artifact
        for content_artifact in dsc_content_artifacts
        if posixpath.dirname(content_artifact.relative_path) == current_dir
    ]
    if len(current_dir_matches) == 1:
        return current_dir_matches[0]
    if dsc_content_artifacts:
        return sorted(
            dsc_content_artifacts, key=lambda content_artifact: content_artifact.relative_path
        )[0]
    return None


def _ensure_source_package_relative_path(source_package, source_packages):
    if source_package.contentartifact_set.filter(
        relative_path=source_package.relative_path
    ).exists():
        return

    dsc_content_artifact = _source_package_dsc_content_artifact(source_package)
    if dsc_content_artifact is None:
        raise _source_package_conflict_error(
            source_package.source,
            source_package.version,
            source_packages,
        )

    source_package.relative_path = dsc_content_artifact.relative_path
    source_package.save(update_fields=["relative_path"])


def _source_package_artifact_debug(source_packages):
    debug_info = []
    for source_package in source_packages:
        rows = (
            source_package.contentartifact_set.select_related("artifact")
            .order_by("relative_path", "artifact__sha256", "artifact__size")
            .values_list("relative_path", "artifact__sha256", "artifact__size")
        )
        debug_info.append(
            {
                "pk": str(source_package.pk),
                "logical_artifacts": tuple(
                    (
                        posixpath.basename(relative_path) if relative_path else None,
                        sha256,
                        size,
                    )
                    for relative_path, sha256, size in rows
                ),
                "relative_paths": tuple(
                    relative_path for relative_path, _, _ in rows if relative_path
                ),
            }
        )
    return debug_info


def _source_package_conflict_error(source, version, source_packages):
    message = _(
        "Cannot create repository version since source packages with the same source/version "
        "have different checksums/artifacts: source='{}', version='{}', duplicates={}"
    )
    return ValueError(
        message.format(source, version, _source_package_artifact_debug(source_packages))
    )


def _choose_canonical_source_package(source_packages, existing_content):
    existing_source_package_ids = set(
        SourcePackage.objects.filter(pk__in=existing_content)
        .filter(pk__in=[source_package.pk for source_package in source_packages])
        .values_list("pk", flat=True)
    )
    for source_package in source_packages:
        if source_package.pk in existing_source_package_ids:
            return source_package
    return source_packages[0]


def _ensure_canonical_source_package_release_components(
    new_version, canonical_source_package, duplicate_source_package, content_qs
):
    duplicate_sprcs = SourcePackageReleaseComponent.objects.filter(
        pk__in=content_qs.filter(pulp_type=SourcePackageReleaseComponent.get_pulp_type()),
        source_package=duplicate_source_package,
    )
    duplicate_sprcs_to_remove = []

    for duplicate_sprc in duplicate_sprcs.select_related("release_component").iterator():
        canonical_sprc, _ = SourcePackageReleaseComponent.objects.get_or_create(
            source_package=canonical_source_package,
            release_component=duplicate_sprc.release_component,
        )
        if not content_qs.filter(pk=canonical_sprc.pk).exists():
            new_version.add_content(
                SourcePackageReleaseComponent.objects.filter(pk=canonical_sprc.pk)
            )
        duplicate_sprcs_to_remove.append(duplicate_sprc.pk)

    if duplicate_sprcs_to_remove:
        new_version.remove_content(
            SourcePackageReleaseComponent.objects.filter(pk__in=duplicate_sprcs_to_remove)
        )


def _ensure_canonical_source_package_content_artifacts(
    canonical_source_package, duplicate_source_package, source_packages
):
    duplicate_content_artifacts = duplicate_source_package.contentartifact_set.select_related(
        "artifact"
    )

    for duplicate_content_artifact in duplicate_content_artifacts.iterator():
        canonical_content_artifact, created = ContentArtifact.objects.get_or_create(
            content=canonical_source_package,
            relative_path=duplicate_content_artifact.relative_path,
            defaults={"artifact": duplicate_content_artifact.artifact},
        )
        if (
            not created
            and canonical_content_artifact.artifact_id != duplicate_content_artifact.artifact_id
        ):
            canonical_artifact = canonical_content_artifact.artifact
            duplicate_artifact = duplicate_content_artifact.artifact
            if (
                canonical_artifact is None
                or duplicate_artifact is None
                or canonical_artifact.sha256 != duplicate_artifact.sha256
                or canonical_artifact.size != duplicate_artifact.size
            ):
                raise _source_package_conflict_error(
                    duplicate_source_package.source,
                    duplicate_source_package.version,
                    source_packages,
                )


def handle_duplicate_source_packages(new_version):
    """
    Deduplicate equivalent SourcePackage content by source/version.

    Some upstream repositories publish the same source package in more than one release/component.
    Pulp repository versions cannot contain duplicate SourcePackage repo keys, so equivalent source
    packages are collapsed to one canonical SourcePackage while keeping each required
    SourcePackageReleaseComponent relationship. Source packages with the same source/version but
    different artifacts are rejected.
    """
    content_qs = new_version.content
    source_package_qs = SourcePackage.objects.filter(
        pk__in=content_qs.filter(pulp_type=SourcePackage.get_pulp_type())
    )
    duplicate_source_packages = (
        source_package_qs.values("source", "version")
        .annotate(count=models.Count("pk"))
        .filter(count__gt=1)
    )

    if not duplicate_source_packages.count():
        return

    existing_content = _get_existing_content(new_version)
    source_packages_to_remove = []

    for source_package_fields in duplicate_source_packages.iterator():
        source_packages = list(
            source_package_qs.filter(
                source=source_package_fields["source"],
                version=source_package_fields["version"],
            ).order_by("pk")
        )
        canonical_source_package = _choose_canonical_source_package(
            source_packages, existing_content
        )
        canonical_sha256 = _source_package_sha256(canonical_source_package)
        canonical_artifact_set = _source_package_artifact_set(canonical_source_package)
        if canonical_artifact_set is None:
            raise _source_package_conflict_error(
                source_package_fields["source"], source_package_fields["version"], source_packages
            )

        for source_package in source_packages:
            if source_package.pk == canonical_source_package.pk:
                continue

            source_package_sha256 = _source_package_sha256(source_package)
            if (
                canonical_sha256 is not None
                and source_package_sha256 is not None
                and canonical_sha256 != source_package_sha256
            ):
                raise _source_package_conflict_error(
                    source_package_fields["source"],
                    source_package_fields["version"],
                    source_packages,
                )

            source_package_artifact_set = _source_package_artifact_set(source_package)
            if (
                source_package_artifact_set is None
                or canonical_artifact_set != source_package_artifact_set
            ):
                raise _source_package_conflict_error(
                    source_package_fields["source"],
                    source_package_fields["version"],
                    source_packages,
                )

            _ensure_canonical_source_package_content_artifacts(
                canonical_source_package, source_package, source_packages
            )
            _ensure_source_package_relative_path(canonical_source_package, source_packages)
            _ensure_canonical_source_package_release_components(
                new_version, canonical_source_package, source_package, content_qs
            )
            source_packages_to_remove.append(source_package.pk)

    if source_packages_to_remove:
        log.warning(_("Removing duplicate deb.source_package from new repo version."))
        new_version.remove_content(SourcePackage.objects.filter(pk__in=source_packages_to_remove))


def handle_duplicate_releases(new_version):
    """
    it may happen that Releases with the same 'distribution' get added.
    E.g. the uniqueness values of the Release (codename, etc) change over time but the
    distribution value stays the same. If content is now copied from newer versions to older
    versions, the new release will be marked for copying but will clash with the value from the
    base-version.
    """
    release_qs_added = new_version.added(base_version=new_version.base_version).filter(
        pulp_type="deb.release"
    )
    if new_version.base_version:
        release_qs_existing = new_version.base_version.content.filter(pulp_type="deb.release")
    else:
        try:
            release_qs_existing = new_version.previous().content.filter(pulp_type="deb.release")
        except new_version.DoesNotExist:
            release_qs_existing = Release.objects.none()

    if not release_qs_added.count():
        # let's assume the previous version is valid
        return

    dup_releases = []
    for new_rel in release_qs_added.iterator():
        if (
            Release.objects.filter(pk__in=release_qs_existing.filter(pk__ne=new_rel.pk))
            .filter(distribution=new_rel.deb_release.distribution)
            .count()
        ):
            # duplicate found: remove it!
            dup_releases.append(new_rel.pk)

    if dup_releases:
        log.warning(_("Removing duplicate deb.releases from new repo version."))
        new_version.remove_content(Release.objects.filter(pk__in=dup_releases))
