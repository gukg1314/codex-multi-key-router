//go:build installer_payload

package main

import _ "embed"

//go:embed payload.zip
var payload []byte
