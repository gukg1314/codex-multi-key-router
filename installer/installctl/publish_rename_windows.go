//go:build windows

package main

import (
	"errors"
	"os"
	"time"

	"golang.org/x/sys/windows"
)

func publishStage(stage, target string) error {
	const maxAttempts = 31
	const delay = 100 * time.Millisecond
	start := time.Now()
	var firstError error
	attempts, err := retryRename(stage, target, func(source, destination string) error {
		renameErr := os.Rename(source, destination)
		if renameErr != nil && firstError == nil {
			firstError = renameErr
		}
		return renameErr
	}, func(err error) bool {
		return errors.Is(err, windows.ERROR_ACCESS_DENIED) || errors.Is(err, windows.ERROR_SHARING_VIOLATION)
	}, maxAttempts, delay)
	if err != nil {
		logf("publish rename failed attempts=%d elapsed=%s first_win32=%d final_win32=%d source=%s target=%s error=%v",
			attempts, time.Since(start), win32Code(firstError), win32Code(err), stage, target, err)
	} else if attempts > 1 {
		logf("publish rename recovered attempts=%d elapsed=%s first_win32=%d source=%s target=%s",
			attempts, time.Since(start), win32Code(firstError), stage, target)
	}
	return err
}
