package psimplatform

// A YAML subset, enough for a subsystem spec and nothing more: block
// mappings and sequences by indentation, flow mappings {a: b} and sequences
// [a, b] on one line, scalars (quoted strings, ints, floats, true/false,
// null), comments. No anchors, no multi-line scalars, no tags — a spec that
// needs those has stopped being a spec.

import (
	"fmt"
	"strconv"
	"strings"
)

type yline struct {
	indent int
	text   string
}

func ParseYAML(src string) (any, error) {
	var lines []yline
	for _, raw := range strings.Split(src, "\n") {
		t := stripComment(raw)
		if strings.TrimSpace(t) == "" {
			continue
		}
		lines = append(lines, yline{len(t) - len(strings.TrimLeft(t, " ")), strings.TrimSpace(t)})
	}
	v, _, err := parseBlock(lines, 0, -1)
	return v, err
}

func stripComment(s string) string {
	inQ := byte(0)
	for i := 0; i < len(s); i++ {
		c := s[i]
		if inQ != 0 {
			if c == inQ {
				inQ = 0
			}
			continue
		}
		if c == '"' || c == '\'' {
			inQ = c
		} else if c == '#' && (i == 0 || s[i-1] == ' ') {
			return s[:i]
		}
	}
	return s
}

// parseBlock parses the lines from i whose indent is > parent; returns the value and the next index.
func parseBlock(lines []yline, i, parent int) (any, int, error) {
	if i >= len(lines) {
		return nil, i, nil
	}
	indent := lines[i].indent
	if strings.HasPrefix(lines[i].text, "- ") || lines[i].text == "-" {
		var out []any
		for i < len(lines) && lines[i].indent == indent && (strings.HasPrefix(lines[i].text, "- ") || lines[i].text == "-") {
			item := strings.TrimSpace(strings.TrimPrefix(lines[i].text, "-"))
			if item == "" {
				v, next, err := parseBlock(lines, i+1, indent)
				if err != nil {
					return nil, 0, err
				}
				out, i = append(out, v), next
				continue
			}
			if k, val, ok := splitKey(item); ok && !strings.HasPrefix(item, "{") && !strings.HasPrefix(item, "[") {
				// "- key: value" starts a mapping whose further keys are indented past the dash
				m := map[string]any{}
				if err := setKey(m, k, val, lines, &i, indent+2); err != nil {
					return nil, 0, err
				}
				for i < len(lines) && lines[i].indent > indent {
					k2, v2, ok := splitKey(lines[i].text)
					if !ok {
						return nil, 0, fmt.Errorf("yaml: bad line %q", lines[i].text)
					}
					if err := setKey(m, k2, v2, lines, &i, lines[i].indent); err != nil {
						return nil, 0, err
					}
				}
				out = append(out, m)
				continue
			}
			v, err := parseScalar(item)
			if err != nil {
				return nil, 0, err
			}
			out, i = append(out, v), i+1
		}
		return out, i, nil
	}
	m := map[string]any{}
	for i < len(lines) && lines[i].indent == indent {
		k, val, ok := splitKey(lines[i].text)
		if !ok {
			return nil, 0, fmt.Errorf("yaml: bad line %q", lines[i].text)
		}
		if err := setKey(m, k, val, lines, &i, indent); err != nil {
			return nil, 0, err
		}
	}
	return m, i, nil
}

// setKey stores key: val (val may be empty, meaning a nested block follows) and advances *i.
func setKey(m map[string]any, k, val string, lines []yline, i *int, indent int) error {
	*i++
	if val == "" {
		if *i < len(lines) && lines[*i].indent > indent || (*i < len(lines) && strings.HasPrefix(lines[*i].text, "- ") && lines[*i].indent >= indent) {
			v, next, err := parseBlock(lines, *i, indent)
			if err != nil {
				return err
			}
			m[k], *i = v, next
			return nil
		}
		m[k] = nil
		return nil
	}
	v, err := parseScalar(val)
	if err != nil {
		return err
	}
	m[k] = v
	return nil
}

func splitKey(s string) (string, string, bool) {
	if strings.HasPrefix(s, "{") || strings.HasPrefix(s, "[") || strings.HasPrefix(s, "\"") || strings.HasPrefix(s, "'") {
		return "", "", false
	}
	i := strings.Index(s, ":")
	if i < 0 {
		return "", "", false
	}
	if i+1 < len(s) && s[i+1] != ' ' {
		return "", "", false
	}
	return strings.TrimSpace(s[:i]), strings.TrimSpace(s[i+1:]), true
}

func parseScalar(s string) (any, error) {
	s = strings.TrimSpace(s)
	switch {
	case s == "" || s == "null" || s == "~":
		return nil, nil
	case strings.HasPrefix(s, "{"):
		if !strings.HasSuffix(s, "}") {
			return nil, fmt.Errorf("yaml: unclosed map %q", s)
		}
		m := map[string]any{}
		for _, part := range splitFlow(s[1 : len(s)-1]) {
			k, v, ok := splitKey(part)
			if !ok {
				return nil, fmt.Errorf("yaml: bad map entry %q", part)
			}
			pv, err := parseScalar(v)
			if err != nil {
				return nil, err
			}
			m[k] = pv
		}
		return m, nil
	case strings.HasPrefix(s, "["):
		if !strings.HasSuffix(s, "]") {
			return nil, fmt.Errorf("yaml: unclosed list %q", s)
		}
		out := []any{}
		for _, part := range splitFlow(s[1 : len(s)-1]) {
			v, err := parseScalar(part)
			if err != nil {
				return nil, err
			}
			out = append(out, v)
		}
		return out, nil
	case strings.HasPrefix(s, "\"") && strings.HasSuffix(s, "\"") && len(s) >= 2:
		return strconv.Unquote(s)
	case strings.HasPrefix(s, "'") && strings.HasSuffix(s, "'") && len(s) >= 2:
		return s[1 : len(s)-1], nil
	case s == "true":
		return true, nil
	case s == "false":
		return false, nil
	}
	if n, err := strconv.Atoi(s); err == nil {
		return n, nil
	}
	if f, err := strconv.ParseFloat(s, 64); err == nil {
		return f, nil
	}
	return s, nil
}

func splitFlow(s string) []string {
	var out []string
	depth, start := 0, 0
	inQ := byte(0)
	for i := 0; i < len(s); i++ {
		c := s[i]
		if inQ != 0 {
			if c == inQ {
				inQ = 0
			}
			continue
		}
		switch c {
		case '"', '\'':
			inQ = c
		case '{', '[':
			depth++
		case '}', ']':
			depth--
		case ',':
			if depth == 0 {
				if p := strings.TrimSpace(s[start:i]); p != "" {
					out = append(out, p)
				}
				start = i + 1
			}
		}
	}
	if p := strings.TrimSpace(s[start:]); p != "" {
		out = append(out, p)
	}
	return out
}
