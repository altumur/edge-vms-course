package cluster

// An S3 object store with Signature Version 4, in the standard library —
// the same forty lines as the Python version, verified against the worked
// examples in Amazon's own SigV4 documentation (s3_test.go). The adapter a
// rented cluster uses (М12 Lesson 8), or one whose heartbeats outgrew raft.

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

func sha256hex(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func hmacSHA256(key []byte, msg string) []byte {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(msg))
	return m.Sum(nil)
}

// uriEncode is SigV4's: unreserved characters and '/' kept, everything else
// percent-encoded upper-case (Python's quote(path, safe="/~")).
func uriEncode(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-', c == '_', c == '.', c == '~', c == '/':
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// Sign returns the headers to send, including Authorization. headers must
// already contain the ones you want signed (host is added here).
func Sign(method, host, path, query string, headers map[string]string, payload []byte,
	accessKey, secretKey, region string, now time.Time, service string) map[string]string {
	if service == "" {
		service = "s3"
	}
	now = now.UTC()
	amzDate, date := now.Format("20060102T150405Z"), now.Format("20060102")
	payloadHash := sha256hex(payload)
	h := map[string]string{}
	for k, v := range headers {
		h[strings.ToLower(k)] = strings.TrimSpace(v)
	}
	h["host"], h["x-amz-date"], h["x-amz-content-sha256"] = host, amzDate, payloadHash
	keys := make([]string, 0, len(h))
	for k := range h {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var canonHeaders strings.Builder
	for _, k := range keys {
		canonHeaders.WriteString(k + ":" + h[k] + "\n")
	}
	signed := strings.Join(keys, ";")
	canonQuery := ""
	if query != "" {
		parts := strings.Split(query, "&")
		sort.Strings(parts)
		canonQuery = strings.Join(parts, "&")
	}
	canonical := strings.Join([]string{method, uriEncode(path), canonQuery, canonHeaders.String(), signed, payloadHash}, "\n")
	scope := fmt.Sprintf("%s/%s/%s/aws4_request", date, region, service)
	toSign := strings.Join([]string{"AWS4-HMAC-SHA256", amzDate, scope, sha256hex([]byte(canonical))}, "\n")
	k := hmacSHA256([]byte("AWS4"+secretKey), date)
	k = hmacSHA256(k, region)
	k = hmacSHA256(k, service)
	k = hmacSHA256(k, "aws4_request")
	signature := hex.EncodeToString(hmacSHA256(k, toSign))
	h["authorization"] = fmt.Sprintf("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s",
		accessKey, scope, signed, signature)
	return h
}

// S3ObjectStore is path-style: <endpoint>/<bucket>/<key>. Credentials from
// the environment, which on a recorder means from its Variable through the
// template — never a file in the image.
type S3ObjectStore struct {
	Scheme, Host, Bucket, Region string
	AccessKey, SecretKey         string
	Client                       *http.Client
	Now                          func() time.Time
}

func NewS3ObjectStore(endpoint, bucket, region, accessKey, secretKey string) (*S3ObjectStore, error) {
	u, err := url.Parse(endpoint)
	if err != nil {
		return nil, err
	}
	if accessKey == "" || secretKey == "" {
		return nil, fmt.Errorf("s3: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are not set")
	}
	return &S3ObjectStore{Scheme: u.Scheme, Host: u.Host, Bucket: bucket, Region: region,
		AccessKey: accessKey, SecretKey: secretKey, Client: &http.Client{Timeout: 10 * time.Second}, Now: time.Now}, nil
}

func (s *S3ObjectStore) request(method, key string, payload []byte) (*http.Response, error) {
	path := "/" + s.Bucket + "/" + key
	headers := Sign(method, s.Host, path, "", nil, payload, s.AccessKey, s.SecretKey, s.Region, s.Now(), "s3")
	var body io.Reader
	if method == "PUT" {
		body = strings.NewReader(string(payload))
	}
	req, err := http.NewRequest(method, s.Scheme+"://"+s.Host+uriEncode(path), body)
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		if k != "host" {
			req.Header.Set(k, v)
		}
	}
	return s.Client.Do(req)
}

func (s *S3ObjectStore) Put(key string, data []byte) error {
	resp, err := s.request("PUT", key, data)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 && resp.StatusCode != 201 && resp.StatusCode != 204 {
		return fmt.Errorf("PUT %s: HTTP %d", key, resp.StatusCode)
	}
	return nil
}

func (s *S3ObjectStore) Get(key string) ([]byte, error) {
	resp, err := s.request("GET", key, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == 404 {
		return nil, nil
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("GET %s: HTTP %d", key, resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

// List: ListObjectsV2 with a prefix — the third call the platform's
// ObjectStore contract needs (heartbeats are found by listing).
func (s *S3ObjectStore) List(prefix string) ([]string, error) {
	path := "/" + s.Bucket + "/"
	query := "list-type=2&prefix=" + url.QueryEscape(prefix)
	headers := Sign("GET", s.Host, path, query, nil, nil, s.AccessKey, s.SecretKey, s.Region, s.Now(), "s3")
	req, err := http.NewRequest("GET", s.Scheme+"://"+s.Host+uriEncode(path)+"?"+query, nil)
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		if k != "host" {
			req.Header.Set(k, v)
		}
	}
	resp, err := s.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("LIST %s: HTTP %d", prefix, resp.StatusCode)
	}
	raw, _ := io.ReadAll(resp.Body)
	var out []string
	for _, part := range strings.Split(string(raw), "<Key>")[1:] {
		key, _, _ := strings.Cut(part, "</Key>")
		out = append(out, key)
	}
	sort.Strings(out)
	return out, nil
}
