//go:build darwin && cgo

package main

/*
#include <stdlib.h>
*/
import "C"

// codexmkQuitRequested is called by the Objective-C side (status item menu,
// Cmd+Q and the app menu "Quit").
//
// The AppKit main thread must not block while the router is stopped, so the
// shared close flow runs on its own goroutine and only the final teardown hops
// back to the main queue.
//
//export codexmkQuitRequested
func codexmkQuitRequested() {
	logger.Printf("menu bar quit requested")
	go quitApp()
}
