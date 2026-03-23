package auth

import "bytes"

func jsonReader(data []byte) *bytes.Reader {
	return bytes.NewReader(data)
}
