#!/bin/sh
# Test-only: a throwaway CA and a server certificate for the dex-idp
# service, so the #1414 IdP check runs over https as Preloop requires.
# Written to the idp-tls volume on every start; never reuse these keys.
set -eu
out=/idp-tls
cd "$out"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=harness idp ca" \
  -keyout ca.key -out ca.pem \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign"
openssl req -newkey rsa:2048 -nodes -subj "/CN=dex-idp" -keyout tls.key -out tls.csr
printf 'subjectAltName=DNS:dex-idp\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' > ext.cnf
openssl x509 -req -in tls.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 2 -out tls.crt -extfile ext.cnf
chmod 0644 ca.pem tls.crt tls.key
