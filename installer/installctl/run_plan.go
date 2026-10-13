package main

import (
	"os"
	"path/filepath"
	"strings"
)

var runValues = []string{"CodexMultiKeyRouter", "CodexOfficialLocalRouter"}

type registryValue struct {
	exists      bool
	stringValue string
	stringType  uint32
	dwordValue  uint32
	isDword     bool
}

type runChange struct {
	before registryValue
	after  registryValue
}

func changedRunNames(changes map[string]runChange) []string {
	var names []string
	for _, name := range runValues {
		if _, changed := changes[name]; changed {
			names = append(names, name)
		}
	}
	return names
}

func runRollbackAllowed(change runChange, current registryValue) bool {
	return change.after.exists == current.exists &&
		(!current.exists || change.after.stringValue == current.stringValue && change.after.stringType == current.stringType)
}

func runValueOwned(value, root string) bool {
	if root == "" {
		return false
	}
	value = strings.TrimSpace(value)
	root = strings.ToLower(filepath.Clean(root)) + string(os.PathSeparator)
	return strings.HasPrefix(strings.ToLower(strings.TrimPrefix(value, `"`)), root)
}

func desiredRunValue(target string) string {
	return `"` + filepath.Join(target, launcherFile) + `" --silent`
}

// A same-directory upgrade does not change any autostart command. The caller
// can skip even opening the shared Run key in that case.
func runMigrationRequired(old, target string) bool {
	return old != "" && !samePath(old, target)
}

func prepareRunMigration(old, target string, read func() (map[string]registryValue, error)) ([]string, error) {
	if !runMigrationRequired(old, target) {
		return nil, nil
	}
	values, err := read()
	if err != nil {
		return nil, err
	}
	return plannedRunNames(old, target, values), nil
}

func plannedRunNames(old, target string, values map[string]registryValue) []string {
	if !runMigrationRequired(old, target) {
		return nil
	}
	var names []string
	for _, name := range runValues {
		value := values[name]
		if !value.exists || value.isDword || !runValueOwned(value.stringValue, old) {
			continue
		}
		if name == runValues[0] && value.stringValue == desiredRunValue(target) {
			continue
		}
		names = append(names, name)
	}
	return names
}
