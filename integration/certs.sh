#!/bin/sh
# A throwaway certificate authority for the integration run: Trino's and
# Keycloak's server certificates, and the client certificate the server's own
# Trino account presents in impersonation mode (mTLS). The CA key is deleted
# once everything is signed.
set -eu
cd /certs
if [ -s ca.crt ]; then echo "certificates already present"; exit 0; fi

openssl req -x509 -newkey rsa:2048 -nodes -days 30 -sha256 \
  -subj "/CN=akko-mcp-trino integration CA" -keyout ca.key -out ca.crt \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"

sign() { # name, extendedKeyUsage, subjectAltName (or empty)
  openssl req -newkey rsa:2048 -nodes -sha256 -subj "/CN=$1" -keyout "$1.key" -out "$1.csr"
  {
    echo "basicConstraints=critical,CA:FALSE"
    echo "keyUsage=critical,digitalSignature,keyEncipherment"
    echo "extendedKeyUsage=$2"
    [ -n "$3" ] && echo "subjectAltName=$3"
  } > "$1.ext"
  openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days 30 -sha256 \
    -extfile "$1.ext" -out "$1.crt"
  rm -f "$1.csr" "$1.ext"
}
sign trino serverAuth "DNS:trino,DNS:localhost"
sign keycloak serverAuth "DNS:keycloak,DNS:localhost"
sign mcp-service clientAuth ""
rm -f ca.key ca.srl

cat trino.key trino.crt > trino.pem && rm -f trino.key
# Trino and Keycloak run as uid 1000, the MCP server as uid 10001.
chown 1000:1000 trino.pem keycloak.key keycloak.crt
chown 10001:10001 mcp-service.key mcp-service.crt
chmod 0400 trino.pem keycloak.key mcp-service.key
chmod 0444 ca.crt trino.crt keycloak.crt mcp-service.crt
echo "integration certificates created"
