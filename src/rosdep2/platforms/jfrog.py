from __future__ import print_function

import os
import hashlib
import subprocess

import yaml

from ..core import rd_debug, InvalidData
from ..installers import PackageManagerInstaller, InstallFailed
from ..shell_utils import create_tempfile_from_string_and_execute
from ..url_utils import urlopen_gzip, URLError

JFROG_INSTALLER = 'jfrog'


def register_installers(context):
    context.set_installer(JFROG_INSTALLER, JFrogInstaller())


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


def _sub_fetch_file(url, md5sum=None):
    """
    Sub-routine of _fetch_file

    :raises: :exc:`DownloadFailed`
    """
    contents = ''
    try:
        s = url.split('/')
        repo = s[4]
        f = "/".join(s[-4:])
        cp = subprocess.run(
            ["jf", "rt", "dl", f"{repo}/{f}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT)
        if cp.returncode != 0:
            raise DownloadFailed(f"jfrog download returned {cp.returncode}")

        with open(f, 'r') as pkg:
            contents = pkg.read().encode('utf-8')
            if md5sum is not None:
                filehash = hashlib.md5(contents).hexdigest()
                if md5sum and filehash != md5sum:
                    raise DownloadFailed("md5sum didn't match for %s.  Expected %s got %s" % (url, md5sum, filehash))

    except DownloadFailed as ex:
        raise DownloadFailed(str(ex))

    return contents


def get_file_hash(filename):
    md5 = hashlib.md5()
    with open(filename, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5.update(chunk)
    return md5.hexdigest()


def fetch_file(url, md5sum=None):
    """
    Download file.  Optionally validate with md5sum

    :param url: URL to download
    :param md5sum: Expected MD5 sum of contents
    """
    error = contents = ''
    try:
        contents = _sub_fetch_file(url, md5sum)
        if not isinstance(contents, str):
            contents = contents.decode('utf-8')
    except DownloadFailed as e:
        rd_debug('Download of file %s failed' % (url))
        error = str(e)
    return contents, error


def load_rdmanifest(contents):
    """
    :raises: :exc:`InvalidRdmanifest`
    """
    try:
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
    contents, error = fetch_file(download_url, md5sum)
    # - try the backup url
    if not contents and alt_url:
        error_prefix = 'Failed to load a rdmanifest from either %s or %s: ' % (url, alt_url)
        download_url = alt_url
        contents, error = fetch_file(download_url, md5sum)
    if not contents:
        raise DownloadFailed(error_prefix + error)
    manifest = load_rdmanifest(contents)
    return manifest, download_url

# TODO: create JFrogInstall instance objects


class JFrogInstall(object):

    def __init__(self):
        self.manifest = self.manifest_url = None
        self.install_command = self.check_presence_command = None
        self.exec_path = None
        self.tarball = self.alternate_tarball = None
        self.tarball_md5sum = None
        self.dependencies = None

    @staticmethod
    def from_manifest(manifest, manifest_url):
        r = JFrogInstall()
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


class JFrogInstaller(PackageManagerInstaller):

    def __init__(self):
        super(JFrogInstaller, self).__init__(source_detect, supports_depends=True)
        self._rdmanifest_cache = {}

    def resolve(self, rosdep_args):
        """
        :raises: :exc:`InvalidData` If format invalid or unable
          to retrieve rdmanifests.
        :returns: [JFrogInstall] instances.
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
            manifest, download_url = download_rdmanifest(url, md5sum, alt_url)
            resolved = JFrogInstall.from_manifest(manifest, download_url)
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
            commands.append(['rosdep-jfrog', 'install', p.manifest_url])
        return commands

    def get_depends(self, rosdep_args):
        deps = rosdep_args.get('depends', [])
        for r in self.resolve(rosdep_args):
            deps.extend(r.dependencies)
        return deps


def install_from_file(rdmanifest_file):
    with open(rdmanifest_file, 'r') as f:
        contents = f.read()
    manifest = load_rdmanifest(contents)
    install_source(JFrogInstall.from_manifest(manifest, rdmanifest_file))


def install_from_url(rdmanifest_url):
    manifest, download_url = download_rdmanifest(rdmanifest_url, None, None)
    install_source(JFrogInstall.from_manifest(manifest, download_url))


def jfrog_urlretrieve(url):
    try:
        s = url.split('/')
        repo = s[4]
        f = "/".join(s[-4:])
        cp = subprocess.run(
            ["jf", "rt", "dl", f"{repo}/{f}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT)
        if cp.returncode == 0:
            return (f, '')

        return ('', '')
    except:
        return ('', '')


def install_source(resolved):
    import shutil
    import tarfile

    rd_debug('Fetching tarball %s' % resolved.tarball)

    s = resolved.tarball.split('/')
    repo = s[4]
    filename = "/".join(s[-4:])
    tempdir = os.path.dirname(filename)
    f = jfrog_urlretrieve(resolved.tarball)
    assert f[0] == filename

    if resolved.tarball_md5sum:
        rd_debug('checking md5sum on tarball')
        hash1 = get_file_hash(filename)
        if resolved.tarball_md5sum != hash1:
            # try backup tarball if it is defined
            if resolved.alternate_tarball:
                f = jfrog_urlretrieve(resolved.alternate_tarball)
                filename = f[0]
                hash2 = get_file_hash(filename)
                if resolved.tarball_md5sum != hash2:
                    failure = (JFROG_INSTALLER, 'md5sum check on %s and %s failed.  Expected %s got %s and %s' % (resolved.tarball, resolved.alternate_tarball, resolved.tarball_md5sum, hash1, hash2))
                    raise InstallFailed(failure=failure)
            else:
                raise InstallFailed((JFROG_INSTALLER, 'md5sum check on %s failed.  Expected %s got %s ' % (resolved.tarball, resolved.tarball_md5sum, hash1)))
    else:
        rd_debug('No md5sum defined for tarball, not checking.')

    try:
        # This is a bit hacky.  Basically, don't unpack dmg files as
        # we are currently using source rosdeps for Nvidia Cg.
        if not filename.endswith('.dmg'):
            rd_debug('Extracting tarball')
            tarf = tarfile.open(filename)
            tarf.extractall(tempdir)
            tarf.close()
        else:
            rd_debug('Bypassing tarball extraction as it is a dmg')
        rd_debug('Running installation script')
        success = create_tempfile_from_string_and_execute(resolved.install_command, os.path.join(tempdir, resolved.exec_path))

        if success:
            rd_debug('successfully executed script')
        else:
            raise InstallFailed((JFROG_INSTALLER, 'installation script returned with error code'))

    finally:
        rd_debug('cleaning up tmpdir [%s]' % (tempdir))
        shutil.rmtree(tempdir)
