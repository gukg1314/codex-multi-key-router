//go:build !windows

package main

import "os"

func publishStage(stage, target string) error { return os.Rename(stage, target) }
