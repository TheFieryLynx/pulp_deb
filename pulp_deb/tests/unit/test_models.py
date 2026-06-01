import posixpath

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from pulpcore.plugin.models import Artifact, Content, ContentArtifact

from pulp_deb.app.models import (
    AptRepository,
    Package,
    PackageReleaseComponent,
    Release,
    ReleaseComponent,
    SourcePackage,
    SourcePackageReleaseComponent,
)
from pulp_deb.app.models.repository import handle_duplicate_releases
from pulp_deb.app.serializers import Package822Serializer


class TestPackage(TestCase):
    """Test Package content type."""

    PACKAGE_PARAGRAPH = (
        "Package: aegir\n"
        "Version: 0.1-edda0\n"
        "Architecture: sea\n"
        "Essential: yes\n"
        "Maintainer: Utgardloki\n"
        "Description: A sea jötunn associated with the ocean.\n"
        "MD5sum: aabb\n"
        "SHA1: ccdd\n"
        "SHA256: eeff\n"
        "Size: 42\n"
        "Filename: pool/a/aegir/aegir_0.1-edda0_sea.deb\n"
    )

    def setUp(self):
        """Setup database fixtures."""
        self.package1 = Package(
            package="aegir",
            version="0.1-edda0",
            architecture="sea",
            essential=True,
            maintainer="Utgardloki",
            description="A sea jötunn associated with the ocean.",
        )
        self.package1.save()
        self.artifact1 = Artifact(
            size=42,
            md5="aabb",
            sha1="ccdd",
            sha256="eeff",
            sha512="kkll",
            file=SimpleUploadedFile("test_filename", b"test content"),
        )
        self.artifact1.save()
        ContentArtifact(artifact=self.artifact1, content=self.package1).save()

    def test_package_fields(self):
        """Test package fields that typically identify a package."""
        self.assertEqual(str(self.package1.package), "aegir")
        self.assertEqual(str(self.package1.version), "0.1-edda0")

    def test_filename(self):
        """Test that the pool filename of a package is correct."""
        self.assertEqual(self.package1.filename(), "pool/a/aegir/aegir_0.1-edda0_sea.deb")

    def test_filename_with_component(self):
        """Test that the pool filename of a package with component is correct."""
        self.assertEqual(
            self.package1.filename("joetunn"), "pool/joetunn/a/aegir/aegir_0.1-edda0_sea.deb"
        )

    def test_to822(self):
        """Test if package transforms correctly into 822dict."""
        artifact_dict = {self.package1.sha256: self.artifact1}
        package_dict = Package822Serializer(self.package1, context={"request": None}).to822(
            "joetunn", artifact_dict=artifact_dict
        )
        self.assertEqual(package_dict["package"], self.package1.package)
        self.assertEqual(package_dict["version"], self.package1.version)
        self.assertEqual(package_dict["architecture"], self.package1.architecture)
        self.assertEqual(package_dict["maintainer"], self.package1.maintainer)
        self.assertEqual(package_dict["description"], self.package1.description)
        self.assertEqual(package_dict["md5sum"], self.artifact1.md5)
        self.assertEqual(package_dict["sha1"], self.artifact1.sha1)
        self.assertEqual(package_dict["sha256"], self.artifact1.sha256)
        self.assertEqual(package_dict["filename"], self.package1.filename("joetunn"))

    def test_to822_dump(self):
        """Test dump to package index."""
        artifact_dict = {self.package1.sha256: self.artifact1}
        self.assertEqual(
            Package822Serializer(self.package1, context={"request": None})
            .to822(artifact_dict=artifact_dict)
            .dump(),
            self.PACKAGE_PARAGRAPH,
        )


class TestRepositoryFunctions(TestCase):
    """Test Repository functions."""

    def create_artifact(self, name, sha256, size=42):
        artifact, _ = Artifact.objects.get_or_create(
            sha256=sha256,
            defaults={
                "size": size,
                "md5": sha256[:32],
                "sha1": sha256[:40],
                "sha512": sha256.ljust(128, sha256[0]),
                "file": SimpleUploadedFile(name, b"test content"),
            },
        )
        return artifact

    def create_source_package(
        self,
        source="aegir",
        version="0.1-edda0",
        directory="pool/a/aegir",
        artifacts=None,
        relative_path=None,
    ):
        if artifacts is None:
            artifacts = (
                (f"{source}_{version}.dsc", "a" * 64, 42),
                (f"{source}_{version}.tar.xz", "b" * 64, 43),
            )
        dsc_name = next(name for name, _, _ in artifacts if name.endswith(".dsc"))
        source_package = SourcePackage.objects.create(
            relative_path=relative_path or posixpath.join(directory, dsc_name),
            format="3.0 (quilt)",
            source=source,
            version=version,
            maintainer="Utgardloki",
            standards_version="4.6.0",
        )
        for name, sha256, size in artifacts:
            artifact = self.create_artifact(name, sha256, size)
            ContentArtifact.objects.create(
                artifact=artifact,
                content=source_package,
                relative_path=posixpath.join(directory, name),
            )
        return source_package

    def test_handle_duplicate_source_packages_preserves_release_components(self):
        repo = AptRepository.objects.create(name="dummy")
        self.addCleanup(repo.delete)
        release_component1 = ReleaseComponent.objects.create(
            distribution="ginnungagap", component="joetunn"
        )
        release_component2 = ReleaseComponent.objects.create(
            distribution="utgard", component="aesir"
        )
        artifacts = (
            (
                "ocl-icd_2.2.14-3.dsc",
                "42a6220a89574a8fc8f1014617c003953b486068b1b6a485aabdde99211f673c",
                2235,
            ),
            (
                "ocl-icd_2.2.14.orig.tar.gz",
                "46df23608605ad548e80b11f4ba0e590cef6397a079d2f19adf707a7c2fbfe1b",
                100629,
            ),
            (
                "ocl-icd_2.2.14-3.debian.tar.xz",
                "b36bc112889e645306044ad13ffc561d0141dfa62da56c73ee04f00fdd932fff",
                12140,
            ),
        )
        source_package1 = self.create_source_package(
            source="ocl-icd",
            version="2.2.14-3",
            directory="pool/universe/o/ocl-icd",
            artifacts=artifacts,
            relative_path="pool/universe/o/ocl-icd/blah",
        )
        source_package2 = self.create_source_package(
            source="ocl-icd",
            version="2.2.14-3",
            directory="pool/main/o/ocl-icd",
            artifacts=artifacts,
            relative_path="pool/main/o/ocl-icd/blah",
        )
        sprc1 = SourcePackageReleaseComponent.objects.create(
            source_package=source_package1,
            release_component=release_component1,
        )
        sprc2 = SourcePackageReleaseComponent.objects.create(
            source_package=source_package2,
            release_component=release_component2,
        )

        with repo.new_version() as version:
            version.add_content(
                Content.objects.filter(
                    pk__in=[
                        release_component1.pk,
                        release_component2.pk,
                        source_package1.pk,
                        source_package2.pk,
                        sprc1.pk,
                        sprc2.pk,
                    ]
                )
            )

        content_qs = repo.latest_version().content
        source_packages = SourcePackage.objects.filter(
            pk__in=content_qs.filter(pulp_type=SourcePackage.get_pulp_type())
        )
        sprcs = SourcePackageReleaseComponent.objects.filter(
            pk__in=content_qs.filter(pulp_type=SourcePackageReleaseComponent.get_pulp_type())
        )

        self.assertEqual(1, source_packages.count())
        self.assertEqual(2, sprcs.count())
        canonical_source_package = source_packages.get()
        self.assertEqual(
            {release_component1.pk, release_component2.pk},
            set(sprcs.values_list("release_component_id", flat=True)),
        )
        self.assertEqual(
            {canonical_source_package.pk},
            set(sprcs.values_list("source_package_id", flat=True)),
        )
        self.assertEqual(
            {
                "pool/universe/o/ocl-icd/ocl-icd_2.2.14-3.dsc",
                "pool/universe/o/ocl-icd/ocl-icd_2.2.14.orig.tar.gz",
                "pool/universe/o/ocl-icd/ocl-icd_2.2.14-3.debian.tar.xz",
                "pool/main/o/ocl-icd/ocl-icd_2.2.14-3.dsc",
                "pool/main/o/ocl-icd/ocl-icd_2.2.14.orig.tar.gz",
                "pool/main/o/ocl-icd/ocl-icd_2.2.14-3.debian.tar.xz",
            },
            set(
                ContentArtifact.objects.filter(content=canonical_source_package).values_list(
                    "relative_path", flat=True
                )
            ),
        )
        self.assertIn(
            canonical_source_package.relative_path,
            {
                "pool/universe/o/ocl-icd/ocl-icd_2.2.14-3.dsc",
                "pool/main/o/ocl-icd/ocl-icd_2.2.14-3.dsc",
            },
        )
        self.assertEqual(
            "42a6220a89574a8fc8f1014617c003953b486068b1b6a485aabdde99211f673c",
            canonical_source_package.sha256,
        )

    def test_handle_duplicate_source_packages_rejects_different_artifacts(self):
        repo = AptRepository.objects.create(name="dummy")
        self.addCleanup(repo.delete)
        release_component1 = ReleaseComponent.objects.create(
            distribution="ginnungagap", component="joetunn"
        )
        release_component2 = ReleaseComponent.objects.create(
            distribution="utgard", component="aesir"
        )
        source_package1 = self.create_source_package(
            artifacts=(
                ("aegir_0.1-edda0.dsc", "a" * 64, 42),
                ("aegir_0.1-edda0.tar.xz", "b" * 64, 43),
            )
        )
        source_package2 = self.create_source_package(
            artifacts=(
                ("aegir_0.1-edda0.dsc", "c" * 64, 42),
                ("aegir_0.1-edda0.tar.xz", "b" * 64, 43),
            )
        )
        sprc1 = SourcePackageReleaseComponent.objects.create(
            source_package=source_package1,
            release_component=release_component1,
        )
        sprc2 = SourcePackageReleaseComponent.objects.create(
            source_package=source_package2,
            release_component=release_component2,
        )

        with self.assertRaisesRegex(
            ValueError,
            r"source packages with the same source/version have different checksums/artifacts:"
            r".*source='aegir'.*version='0.1-edda0'.*logical_artifacts",
        ):
            with repo.new_version() as version:
                version.add_content(
                    Content.objects.filter(
                        pk__in=[
                            release_component1.pk,
                            release_component2.pk,
                            source_package1.pk,
                            source_package2.pk,
                            sprc1.pk,
                            sprc2.pk,
                        ]
                    )
                )

    def test_handle_duplicate_source_packages_rejects_different_file_sets(self):
        repo = AptRepository.objects.create(name="dummy")
        self.addCleanup(repo.delete)
        release_component1 = ReleaseComponent.objects.create(
            distribution="ginnungagap", component="joetunn"
        )
        release_component2 = ReleaseComponent.objects.create(
            distribution="utgard", component="aesir"
        )
        source_package1 = self.create_source_package(
            artifacts=(
                ("aegir_0.1-edda0.dsc", "a" * 64, 42),
                ("aegir_0.1-edda0.orig.tar.gz", "b" * 64, 43),
            )
        )
        source_package2 = self.create_source_package(
            artifacts=(
                ("aegir_0.1-edda0.dsc", "a" * 64, 42),
                ("aegir_0.1-edda0.orig.tar.gz", "b" * 64, 43),
                ("aegir_0.1-edda0.debian.tar.xz", "c" * 64, 44),
            )
        )
        sprc1 = SourcePackageReleaseComponent.objects.create(
            source_package=source_package1,
            release_component=release_component1,
        )
        sprc2 = SourcePackageReleaseComponent.objects.create(
            source_package=source_package2,
            release_component=release_component2,
        )

        with self.assertRaisesRegex(
            ValueError,
            r"source packages with the same source/version have different checksums/artifacts:"
            r".*source='aegir'.*version='0.1-edda0'.*logical_artifacts",
        ):
            with repo.new_version() as version:
                version.add_content(
                    Content.objects.filter(
                        pk__in=[
                            release_component1.pk,
                            release_component2.pk,
                            source_package1.pk,
                            source_package2.pk,
                            sprc1.pk,
                            sprc2.pk,
                        ]
                    )
                )

    def test_package_duplicates_with_different_checksums_still_fail(self):
        repo = AptRepository.objects.create(name="dummy")
        self.addCleanup(repo.delete)
        release_component = ReleaseComponent.objects.create(
            distribution="ginnungagap", component="joetunn"
        )
        package1 = Package.objects.create(
            package="aegir",
            version="0.1-edda0",
            architecture="sea",
            maintainer="Utgardloki",
            description="A sea jötunn associated with the ocean.",
            relative_path="pool/a/aegir/aegir_0.1-edda0_sea.deb",
            sha256="a" * 64,
        )
        package2 = Package.objects.create(
            package="aegir",
            version="0.1-edda0",
            architecture="sea",
            maintainer="Utgardloki",
            description="A sea jötunn associated with the ocean.",
            relative_path="pool/a/aegir/aegir_0.1-edda0_sea-duplicate.deb",
            sha256="b" * 64,
        )
        prc1 = PackageReleaseComponent.objects.create(
            package=package1, release_component=release_component
        )
        prc2 = PackageReleaseComponent.objects.create(
            package=package2, release_component=release_component
        )

        with self.assertRaisesRegex(
            ValueError,
            "same name, version, and architecture, but a different checksum",
        ):
            with repo.new_version() as version:
                version.add_content(
                    Content.objects.filter(
                        pk__in=[
                            release_component.pk,
                            package1.pk,
                            package2.pk,
                            prc1.pk,
                            prc2.pk,
                        ]
                    )
                )

    def test_handle_duplicate_releases(self):
        repo = AptRepository.objects.create(name="dummy")
        self.addCleanup(repo.delete)
        rel1 = Release.objects.create(
            distribution="ginnungagap",
            codename="ginnungagap",
            suite="ginnungagap",
            version="ginnungagap",
            origin="norse",
            label="ginnungagap",
            description="ginnungagap",
        )
        rel2 = Release.objects.create(
            distribution="ginnungagap",
        )

        with repo.new_version() as base_version:
            base_version.add_content(Release.objects.filter(pk=rel1.pk))

        with repo.new_version(base_version=repo.latest_version()) as version:
            version.add_content(Release.objects.filter(pk=rel2.pk))
            self.assertEqual(2, version.content.count())

            handle_duplicate_releases(version)
            self.assertEqual(1, version.content.count())
            self.assertEqual(rel1.pk, version.content[0].pk)
