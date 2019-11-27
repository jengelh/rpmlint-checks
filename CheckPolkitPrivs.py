# vim: sw=4 et sts=4 ts=4 :
#############################################################################
# File          : CheckPolkitPrivs.py
# Package       : rpmlint
# Author        : Ludwig Nussel
# Purpose       : Check for /etc/polkit-default-privs violations
#############################################################################

from Filter import *
import AbstractCheck
import Config
import re
import os
import sys
import json
import hashlib
import Whitelisting
from xml.dom.minidom import parse

POLKIT_PRIVS_WHITELIST = Config.getOption('PolkitPrivsWhiteList', ())   # set of file names
POLKIT_PRIVS_FILES = Config.getOption('PolkitPrivsFiles', ["/etc/polkit-default-privs.standard"])
# path to JSON files containing whitelistings for files in rules.d directories
POLKIT_RULES_WHITELIST = Config.getOption('PolkitRulesWhitelist', ())


class PolkitCheck(AbstractCheck.AbstractCheck):
    def __init__(self):
        AbstractCheck.AbstractCheck.__init__(self, "CheckPolkitPrivs")
        self.privs = {}
        self._collect_privs()

        # a structure like this:
        # {
        #   "<package>": {
        #       "skip-digest-check": bool
        #       "<path>": {
        #           "audits": [
        #               {
        #                   "bug": "bsc#4711",
        #                   "comment": "note about whitelisting",
        #                   "digest": "<alg>:<digest>"
        #               }
        #           ]
        #       }
        #   }
        # }
        self.rules = {}
        self._collect_rules_whitelist()

    def _get_err_prefix(self):
        """error prefix label to be used for early error printing."""
        return self.__class__.__name__ + ":"

    def _collect_privs(self):
        for filename in POLKIT_PRIVS_FILES:
            if os.path.exists(filename):
                self._parse_privs_file(filename)

    def _parse_privs_file(self, filename):
        with open(filename) as inputfile:
            for line in inputfile:
                line = line.split('#')[0].split('\n')[0]
                if len(line):
                    line = re.split(r'\s+', line)
                    priv = line[0]
                    value = line[1]

                    self.privs[priv] = value

    def _collect_rules_whitelist(self):
        for filename in POLKIT_RULES_WHITELIST:
            if os.path.exists(filename):
                self._parse_rules_whitelist(filename)

    def _parse_rules_whitelist(self, filename):
        """
        The JSON data is structured like this:

        [
            {
                "package": "polkit-default-privs",
                "path": "/etc/polkit-1/rules.d/90-default-privs.rules",
                # can be left out, default is false
                # if set then the content will not
                # be checked (only to be used for special cases)
                "skip-digest-check": true,
                "audits": [
                    {
                        "bug": "bsc#1125314",
                        "comment": "rules dynamically generated by our own polkit profile tooling",
                        "digest": "sha256:aea3041de2c15db8683620de8533206e50241c309eb27893605d5ead17e5e75f"
                    },
                    {
                        "bug": "bsc#4711",
                        "comment": "no-op changes in comments",
                        "digest": "<alg>:<digest>"
                    }
                ]
            },
            {
                ...
            }
        ]
        """

        try:
            with open(filename, 'r') as fd:
                data = json.load(fd)

                for entry in data:
                    self._parse_rules_whitelist_entry(entry)

        except Exception as e:
            print(self._get_err_prefix(), "failed to parse json file {}: {}".format(
                filename, str(e)),
                file=sys.stderr
            )

    def _parse_rules_whitelist_entry(self, entry):
        path = entry["path"]
        package = entry["package"]
        skip_digest_check = entry.get("skip-digest-check", False)

        audits = entry.get("audits")

        # it is thinkable that the same rules file is shipped by a
        # different conflicting package, therefore support
        # multiple packages claiming the same path
        pkg_dict = self.rules.setdefault(path, {})

        if package in pkg_dict:
            print(self._get_err_prefix(), "duplicate entry for path {} and package {}".format(
                path, package),
                file=sys.stderr
            )
            return

        pkg_dict[package] = {
            "skip-digest-check": skip_digest_check,
            "audits": audits
        }

    def check_perm_files(self, pkg):
        """Checks files in polkit-default-privs.d."""

        files = pkg.files()
        prefix = "/etc/polkit-default-privs.d/"
        profiles = ("restrictive", "standard", "relaxed")

        permfiles = []
        # first pass, find additional files
        for f in files:
            if f in pkg.ghostFiles():
                continue

            if f.startswith(prefix):

                bn = f[len(prefix):]
                if bn not in POLKIT_PRIVS_WHITELIST:
                    printError(pkg, "polkit-unauthorized-file", f)

                parts = bn.rsplit('.', 1)

                if len(parts) == 2 and parts[-1] in profiles:
                    bn = parts[0]

                if bn not in permfiles:
                    permfiles.append(bn)

        for f in sorted(permfiles):
            f = pkg.dirName() + prefix + f

            for profile in profiles:
                path = '.'.join(f, profile)
                if os.path.exists(path):
                    self._parse_privs_file(path)
                    break
            else:
                self._parse_privs_file(f)

    def check_actions(self, pkg):
        """Checks files in the actions directory."""

        files = pkg.files()
        prefix = "/usr/share/polkit-1/actions/"

        for f in files:
            if f in pkg.ghostFiles():
                continue

            # catch xml exceptions
            try:
                if f.startswith(prefix):
                    xml = parse(pkg.dirName() + f)
                    for a in xml.getElementsByTagName("action"):
                        self.check_action(pkg, a)
            except Exception as x:
                printError(pkg, 'rpmlint-exception', "%(file)s raised an exception: %(x)s" % {'file': f, 'x': x})
                continue

    def check_action(self, pkg, action):
        """Inspect a single polkit action used by an application."""
        action_id = action.getAttribute('id')

        if action_id in self.privs:
            # the action is explicitly whitelisted, nothing else to do
            return

        allow_types = ('allow_any', 'allow_inactive', 'allow_active')
        foundunauthorized = False
        foundno = False
        foundundef = False
        settings = {}
        try:
            defaults = action.getElementsByTagName("defaults")[0]
            for i in defaults.childNodes:
                if not i.nodeType == i.ELEMENT_NODE:
                    continue

                if i.nodeName in allow_types:
                    settings[i.nodeName] = i.firstChild.data
        except KeyError:
            foundunauthorized = True

        for i in allow_types:
            if i not in settings:
                foundundef = True
                settings[i] = '??'
            elif settings[i].find("auth_admin") != 0:
                if settings[i] == 'no':
                    foundno = True
                else:
                    foundunauthorized = True

        action_settings = "{} ({}:{}:{})".format(
            action_id,
            *(settings[type] for type in allow_types)
        )

        if foundunauthorized:
            printError(
                pkg, 'polkit-unauthorized-privilege', action_settings)
        else:
            printError(
                pkg, 'polkit-untracked-privilege', action_settings)

        if foundno or foundundef:
            printInfo(
                pkg, 'polkit-cant-acquire-privilege', action_settings)

    def check_rules(self, pkg):
        """Process files and whitelist for entries in rules.d dirs."""

        files = pkg.files()
        rule_dirs = ("/etc/polkit-1/rules.d/", "/usr/share/polkit-1/rules.d/")

        for f in files:
            if f in pkg.ghostFiles():
                continue

            for rule_dir in rule_dirs:
                if f.startswith(rule_dir):
                    break
            else:
                # no match
                continue

            pkgs = self.rules.get(f, None)
            wl_entry = pkgs.get(pkg.name, None) if pkgs else None

            if not pkgs or not wl_entry:
                # no whitelist entry exists for this file
                printError(pkg, 'polkit-unauthorized-rules', f)
                continue

            if wl_entry["skip-digest-check"]:
                # for this package/file combination no file content digest
                # verification needs to be performed, so we're already fine
                continue

            # check the newest entry first it is more likely to match what we
            # have
            for audit in reversed(wl_entry["audits"]):
                digest_matches = self._checkDigest(pkg, f, audit["digest"])

                if digest_matches:
                    break
            else:
                # none of the digest entries matched
                printError(pkg, 'polkit-changed-rules', f)
                continue

    def _checkDigest(self, pkg, path, digest_spec):
        if not digest_spec:
            return False

        parts = digest_spec.split(':', 1)
        if len(parts) != 2:
            print(self._get_err_prefix(), "bad digest specification for package {} file {}".format(
                pkg.name, path),
                file=sys.stderr
            )
            return False

        alg, digest = parts

        try:
            h = hashlib.new(alg)
        except ValueError:
            print(self._get_err_prefix(), "bad digest algorithm '{}' for package {} file {}".format(
                alg, pkg.name, path),
                file=sys.stderr
            )
            return False

        with open(pkg.dirName() + path, 'rb') as fd:
            while True:
                data = fd.read(4096)
                if not data:
                    break

                h.update(data)

        return h.hexdigest() == digest

    def check(self, pkg):

        if pkg.isSource():
            return

        self.check_perm_files(pkg)
        self.check_actions(pkg)
        self.check_rules(pkg)


check = PolkitCheck()

for _id, desc in (
        (
            'polkit-unauthorized-file',
            """A custom polkit rule file is installed by this package. If the package is
            intended for inclusion in any SUSE product please open a bug report to request
            review of the package by the security team. Please refer to {url} for more
            information"""
        ),
        (
            'polkit-unauthorized-privilege',
            """The package allows unprivileged users to carry out privileged
            operations without authentication. This could cause security
            problems if not done carefully. If the package is intended for
            inclusion in any SUSE product please open a bug report to request
            review of the package by the security team. Please refer to {url}
            for more information."""
        ),
        (
            'polkit-untracked-privilege',
            """The privilege is not listed in /etc/polkit-default-privs.*
            which makes it harder for admins to find. Furthermore polkit
            authorization checks can easily introduce security issues. If the
            package is intended for inclusion in any SUSE product please open
            a bug report to request review of the package by the security team.
            Please refer to {url} for more information."""
        ),
        (
            'polkit-cant-acquire-privilege',
            """Usability can be improved by allowing users to acquire privileges
            via authentication. Use e.g. 'auth_admin' instead of 'no' and make
            sure to define 'allow_any'. This is an issue only if the privilege
            is not listed in /etc/polkit-default-privs.*"""
        ),
        (
            'polkit-unauthorized-rules',
            """A polkit rules file installed by this package is not whitelisted in the
            polkit-whitelisting package. If the package is intended for inclusion in any
            SUSE product please open a bug report to request review of the package by the
            security team. Please refer to {url} for more information."""
        ),
        (
            'polkit-changed-rules',
            """A polkit rules file installed by this package changed in content. Please
            open a bug report to request follow-up review of the introduced changes by
            the security team. Please refer to {url} for more information."""
        )
):
    addDetails(_id, desc.format(url=Whitelisting.AUDIT_BUG_URL))
