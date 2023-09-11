"""
Script for installing rdmanifest-described resources from jfrog
"""

from __future__ import print_function

import os
import sys
from optparse import OptionParser

from rosdep2 import InstallFailed
from rosdep2.platforms import jfrog

NAME = 'rosdep-jfrog'


def install_main():
    parser = OptionParser(usage="usage: %prog install <rdmanifest-url>", prog=NAME)
    options, args = parser.parse_args()
    if len(args) != 2:
        parser.error("please specify one and only one rdmanifest url")
    if args[0] != 'install':
        parser.error("currently only support the 'install' command")
    rdmanifest_url = args[1]
    try:
        if os.path.isfile(rdmanifest_url):
            jfrog.install_from_file(rdmanifest_url)
        else:
            jfrog.install_from_url(rdmanifest_url)
    except InstallFailed as e:
        print("ERROR: installation failed:\n%s" % e, file=sys.stderr)
        sys.exit(1)
