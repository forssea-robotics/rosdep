from __future__ import print_function

import os
import hashlib
import subprocess
import tempfile
import yaml

from ..core import rd_debug, InvalidData
from ..installers import PackageManagerInstaller, InstallFailed
from ..shell_utils import create_tempfile_from_string_and_execute
from ..url_utils import urlopen_gzip, URLError

HARNESS_INSTALLER = 'harness'


def register_installers(context):
    context.set_installer(HARNESS_INSTALLER, HarnessInstaller())


class InvalidRdmanifest(Exception):
    """
    rdmanifest format is invalid.
    """
    pass


class DownloadFailed(Exception):
    """
    File download failed either due to i/o issues or md5sum validation.
    """
    pass


def fetch_file(url, md5sum=None):
    """
    URI format: https://pkg.harness.io/pkg/<ACCOUNT>/<REGISTRY>/files/<ARTIFACT>/<VERSION>/<FILEPATH>

    In general, FILE and ARTIFACT are the same for single files.

    :raises: :exc:`DownloadFailed`
    """
    try:
        s = url.split('/')
        registry = s[5]
        artifact = s[7]
        version = s[8]
        filename = '/'.join(s[9:])
        file_dir = os.path.dirname(filename)

        tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmpdir, file_dir), exist_ok=True)

        cmds = ["harness", "pull", "artifact", f"{registry}/{artifact}", ".", "--version", version, "--filename", filename, "--package-type", "generic"]
        cp = subprocess.run(
            cmds,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=tmpdir
        )
        if cp.returncode != 0:
            out = cp.stdout.decode('utf-8', errors='replace')
            raise DownloadFailed(f"harness download returned {cp.returncode}: {out}")

        if md5sum is not None:
            path = os.path.join(tmpdir, filename)
            try:
                with open(path, 'rb') as f:
                    contents = f.read()
            except OSError as ex:
                raise DownloadFailed(f"could not read downloaded file {path}: {ex}")

            if md5sum and hashlib.md5(contents).hexdigest() != md5sum:
                raise DownloadFailed(f"md5sum didn't match for {url}. "
                    f"Expected {md5sum} got {hashlib.md5(contents).hexdigest()}")

    except DownloadFailed as ex:
        raise DownloadFailed(str(ex))

    return registry, artifact, version, tmpdir, filename


def get_file_hash(filename):
    md5 = hashlib.md5()
    with open(filename, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5.update(chunk)
    return md5.hexdigest()


def load_rdmanifest(rdmanifest_file):
    """
    :raises: :exc:`InvalidRdmanifest`
    """
    try:
        with open(rdmanifest_file, 'r') as f:
            contents = f.read()
            if not isinstance(contents, str):
                contents = contents.decode('utf-8')
            return yaml.safe_load(contents)
    except yaml.scanner.ScannerError as ex:
        raise InvalidRdmanifest('Failed to parse yaml in %s:  Error: %s' % (contents, ex))


def download_rdmanifest(url, md5sum, alt_url=None):
    """
    :param url: URL to download rdmanifest from
    :param md5sum: MD5 sum for validating url download, or None

    :returns: (contents of rdmanifest, download_url).  download_url is
      either *url* or *alt_url* and indicates which of the locations
      contents was generated from.
    :raises: :exc:`DownloadFailed`
    :raises: :exc:`InvalidRdmanifest`
    """
    # fetch the manifest
    download_url = url
    error_prefix = 'Failed to load a rdmanifest from %s: ' % (url)
    registry, artifact, version, directory, filename = fetch_file(download_url, md5sum)
    # - try the backup url
    if not filename and alt_url:
        error_prefix = 'Failed to load a rdmanifest from either %s or %s: ' % (url, alt_url)
        download_url = alt_url
        registry, artifact, version, directory, filename = fetch_file(download_url, md5sum)
    if not filename:
        raise DownloadFailed(error_prefix)
    return download_url, registry, artifact, version, '/'.join([directory, filename])

class HarnessInstall(object):

    def __init__(self):
        self.manifest = self.manifest_url = None
        self.install_command = self.check_presence_command = None
        self.exec_path = None
        self.tarball = self.alternate_tarball = None
        self.tarball_md5sum = None
        self.dependencies = None

    @staticmethod
    def from_manifest(manifest, manifest_url):
        r = HarnessInstall()
        r.manifest = manifest
        r.manifest_url = manifest_url
        rd_debug('Loading manifest:\n{{{%s\n}}}\n' % manifest)

        r.install_command = manifest.get('install-script', '')
        r.check_presence_command = manifest.get('check-presence-script', '')

        r.exec_path = manifest.get('exec-path', '.')
        try:
            r.tarball = manifest['uri']
        except KeyError:
            raise InvalidRdmanifest('uri required for source rosdeps')
        r.alternate_tarball = manifest.get('alternate-uri')
        r.tarball_md5sum = manifest.get('md5sum')
        r.dependencies = manifest.get('depends', [])
        return r

    def __str__(self):
        return 'source: %s' % (self.manifest_url)

    __repr__ = __str__


def is_source_installed(source_item, exec_fn=None):
    return create_tempfile_from_string_and_execute(source_item.check_presence_command, exec_fn=exec_fn)


def source_detect(pkgs, exec_fn=None):
    return [x for x in pkgs if is_source_installed(x, exec_fn=exec_fn)]


class HarnessInstaller(PackageManagerInstaller):

    def __init__(self):
        super(HarnessInstaller, self).__init__(source_detect, supports_depends=True)
        self._rdmanifest_cache = {}

    def resolve(self, rosdep_args):
        """
        :raises: :exc:`InvalidData` If format invalid or unable
          to retrieve rdmanifests.
        :returns: [HarnessInstall] instances.
        """
        try:
            url = rosdep_args['uri']
        except KeyError:
            raise InvalidData("'uri' key required for source rosdeps")
        alt_url = rosdep_args.get('alternate-uri', None)
        md5sum = rosdep_args.get('md5sum', None)

        # load manifest from cache or from web
        manifest = None
        if url in self._rdmanifest_cache:
            return self._rdmanifest_cache[url]
        elif alt_url in self._rdmanifest_cache:
            return self._rdmanifest_cache[alt_url]
        try:
            rd_debug('Downloading manifest [%s], mirror [%s]' % (url, alt_url))
            download_url, registry, artifact, version, filename = download_rdmanifest(url, md5sum, alt_url)
            manifest = load_rdmanifest(filename)
            resolved = HarnessInstall.from_manifest(manifest, download_url)
            self._rdmanifest_cache[download_url] = [resolved]
            return [resolved]
        except DownloadFailed as ex:
            # not sure this should be masked this way
            raise InvalidData(str(ex))
        except InvalidRdmanifest as ex:
            raise InvalidData(str(ex))

    def get_install_command(self, resolved, interactive=True, reinstall=False, quiet=False):
        # Instead of attempting to describe the source-install steps
        # inside of the rosdep command chain, we shell out to an
        # external rosdep-jfrog command.  This separation means that
        # users can manually invoke rosdep-jfrog and also keeps
        # 'get_install_command()' cleaner.
        packages = self.get_packages_to_install(resolved, reinstall=reinstall)
        commands = []
        for p in packages:
            commands.append(['rosdep-harness', 'install', p.manifest_url])
        return commands

    def get_depends(self, rosdep_args):
        deps = rosdep_args.get('depends', [])
        for r in self.resolve(rosdep_args):
            deps.extend(r.dependencies)
        return deps


def install_from_file(rdmanifest_file):
    manifest = load_rdmanifest(rdmanifest_file)
    install_source(HarnessInstall.from_manifest(manifest, rdmanifest_file))


def install_from_url(rdmanifest_url):
    download_url, registry, artifact, version, filename = download_rdmanifest(rdmanifest_url, None, None)
    manifest = load_rdmanifest(filename)
    install_source(HarnessInstall.from_manifest(manifest, download_url))


def install_source(resolved):
    import shutil
    import tarfile

    rd_debug('Fetching tarball %s' % resolved.tarball)

    registry, artifact, version, directory, filename = fetch_file(resolved.tarball, resolved.tarball_md5sum)

    try:
        rd_debug('Running installation script')
        success = create_tempfile_from_string_and_execute(resolved.install_command, os.path.join(directory, os.path.dirname(filename), resolved.exec_path))

        if success:
            rd_debug('successfully executed script')
        else:
            raise InstallFailed((HARNESS_INSTALLER, 'installation script returned with error code'))

    finally:
        rd_debug('cleaning up tmpdir [%s]' % (directory))
        shutil.rmtree(directory)
