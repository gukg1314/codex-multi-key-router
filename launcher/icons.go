package main

import (
	_ "embed"
	"encoding/binary"
)

// trayIconBytes is the Windows notification area icon, derived from the shared
// source image by packaging/icons.py. Embedding it keeps the launcher free of
// any loose file next to the exe.
//
//go:embed assets/tray.ico
var trayIconBytes []byte

// icoFrame returns the byte range of one frame inside an ICO file so the
// launcher can hand it to CreateIconFromResourceEx on Windows. The index is
// clamped to the last frame, and an unreadable directory is reported instead of
// panicking: a missing tray icon must never stop the tool from starting.
func icoFrame(data []byte, index int) (int, int, bool) {
	if len(data) < 6 || binary.LittleEndian.Uint16(data[2:4]) == 0 {
		return 0, 0, false
	}
	count := int(binary.LittleEndian.Uint16(data[2:4]))
	if len(data) < 6+16*count {
		return 0, 0, false
	}
	if index >= count {
		index = count - 1
	}
	if index < 0 {
		index = 0
	}
	entry := 6 + 16*index
	offset := int(binary.LittleEndian.Uint32(data[entry+12 : entry+16]))
	size := int(binary.LittleEndian.Uint32(data[entry+8 : entry+12]))
	if size <= 0 || offset < 0 || offset+size > len(data) {
		return 0, 0, false
	}
	return offset, size, true
}
