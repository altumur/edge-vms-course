"""An S3 object store with Signature Version 4, in the standard library.

The appliance image carries no boto3 (Lesson 2's image is small on purpose), and
the restore point only needs two verbs. This is SigV4 for PUT and GET with
the payload hash in the request, against MinIO or S3, path-style addressing.

Verified against the worked example in Amazon's own SigV4 documentation
("Example: GET Object" — bucket examplebucket, key test.txt, 24 May 2013),
which publishes the expected signature; tests/test_s3.py reproduces it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, query: str, headers: dict, payload: bytes,
         access_key: str, secret_key: str, region: str, now: dt.datetime, service: str = "s3") -> dict:
    """Return the headers to send, including Authorization. `headers` must
    already contain the ones you want signed (host is added here)."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = _sha256(payload)
    h = {k.lower(): v.strip() for k, v in headers.items()}
    h["host"] = host
    h["x-amz-date"] = amz_date
    h["x-amz-content-sha256"] = payload_hash
    signed = ";".join(sorted(h))
    canonical_headers = "".join(f"{k}:{h[k]}\n" for k in sorted(h))
    canonical_query = "&".join(sorted(query.split("&"))) if query else ""
    canonical = "\n".join([method, urllib.parse.quote(path, safe="/~"), canonical_query,
                           canonical_headers, signed, payload_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical.encode())])
    k = _hmac(("AWS4" + secret_key).encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                          f"SignedHeaders={signed}, Signature={signature}")
    return h


class S3ObjectStore:
    """Path-style: <endpoint>/<bucket>/<key>. Credentials from the
    environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), which on a server
    means from its Variable through the template — never a file in the image."""

    def __init__(self, endpoint: str, bucket: str, region: str = "us-east-1",
                 access_key: str | None = None, secret_key: str | None = None, timeout: float = 10.0):
        u = urllib.parse.urlsplit(endpoint)
        self.scheme, self.host = u.scheme, u.netloc
        self.bucket, self.region, self.timeout = bucket, region, timeout
        self.access_key = access_key or os.environ["AWS_ACCESS_KEY_ID"]
        self.secret_key = secret_key or os.environ["AWS_SECRET_ACCESS_KEY"]

    def _request(self, method: str, key: str, payload: bytes = b"", query: str = ""):
        path = f"/{self.bucket}/{key}"
        headers = sign(method, self.host, path, query, {}, payload, self.access_key, self.secret_key,
                       self.region, dt.datetime.now(dt.timezone.utc))
        req = urllib.request.Request(f"{self.scheme}://{self.host}{path}" + (f"?{query}" if query else ""),
                                     data=payload if method == "PUT" else None, method=method)
        for k, v in headers.items():
            if k != "host":
                req.add_header(k, v)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def put(self, key: str, data: bytes) -> None:
        with self._request("PUT", key, data) as r:
            if r.status not in (200, 201, 204):
                raise IOError(f"PUT {key}: {r.status}")

    def get(self, key: str) -> bytes | None:
        try:
            with self._request("GET", key) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def list(self, prefix: str) -> list[str]:
        """ListObjectsV2, one page (the heartbeat prefix holds one object per
        worker; a thousand is a page). Keys from the XML by a plain regex —
        the response has no nesting worth a parser."""
        import re
        q = "list-type=2&prefix=" + urllib.parse.quote(prefix, safe="")
        with self._request("GET", "", b"", q) as r:
            body = r.read().decode()
        return sorted(re.findall(r"<Key>([^<]+)</Key>", body))
