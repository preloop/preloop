#!/usr/bin/perl
# Synthetic fixture: needs Perl 5.10 (defined-or) and nothing newer.
use strict;
use warnings;

my %config = (name => 'daemon');
my $port = $config{port} // 8080;
print "$config{name} listens on $port\n";
