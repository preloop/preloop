#!/usr/bin/perl
# Synthetic fixture: postfix dereference needs a Perl newer than 5.10.
# Perl::MinimumVersion 1.40 reports 5.020 here because of the feature
# pragma. It does not detect a bare `->@*` without the pragma (enabled by
# default from Perl 5.24), so this fixture keeps the pragma.
use strict;
use warnings;
use feature 'postderef';
no warnings 'experimental::postderef';

my $hosts = ['alpha', 'beta'];
print join(',', $hosts->@*), "\n";
