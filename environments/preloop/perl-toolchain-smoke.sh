#!/bin/bash
# Fail the image build unless the distro Perl toolchain is usable.
# perlver 1.40 has no --version flag (it treats the option as unknown and
# exits non-zero). Prove the script by running it on a one-line program,
# and print the module version the same way the reviewer fallback does.
set -euo pipefail

command -v perl >/dev/null
command -v cpanm >/dev/null
command -v perlver >/dev/null

perl -MPerl::MinimumVersion -e 'print $Perl::MinimumVersion::VERSION, "\n"'
perl -MPerl::Critic -e 'print $Perl::Critic::VERSION, "\n"'
perl -MTest::More -e 'print $Test::More::VERSION, "\n"'
perl -MTest::Harness -e 'print $Test::Harness::VERSION, "\n"'

perlcritic --version
prove --version

fixture="$(mktemp)"
trap 'rm -f "$fixture"' EXIT
printf '%s\n' 'print "ok\n";' >"$fixture"
perlver "$fixture"
