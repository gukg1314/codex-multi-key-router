//go:build !installer_payload

package main

// The uninstall helper does not need the payload; the setup build adds it
// through the installer_payload tag.
var payload []byte
