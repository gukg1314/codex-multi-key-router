package main

import (
	"errors"
	"path/filepath"
	"testing"
)

func TestSameDirectoryUpgradeNeverReadsRunKey(t *testing.T) {
	old := filepath.Join(t.TempDir(), "CodexMultiKey")
	read := func() (map[string]registryValue, error) {
		t.Fatal("same-directory upgrade must not touch an inaccessible Run key")
		return nil, errors.New("access denied")
	}
	for _, target := range []string{old, filepath.Join(filepath.Dir(old), "CODEXMULTIKEY")} {
		names, err := prepareRunMigration(old, target, read)
		if err != nil || len(names) != 0 {
			t.Fatalf("unexpected Run migration: names=%v error=%v", names, err)
		}
	}
}

func TestRunMigrationOnlyPlansOwnedChangedValues(t *testing.T) {
	old := filepath.Join(t.TempDir(), "old")
	target := filepath.Join(filepath.Dir(old), "new")
	primary := registryValue{exists: true, stringValue: `"` + filepath.Join(old, launcherFile) + `" --silent`}
	legacy := registryValue{exists: true, stringValue: `"` + filepath.Join(old, "runtime", "python", "pythonw.exe") + `"`}
	values := map[string]registryValue{runValues[0]: primary, runValues[1]: legacy}
	names, err := prepareRunMigration(old, target, func() (map[string]registryValue, error) { return values, nil })
	if err != nil || len(names) != 2 || names[0] != runValues[0] || names[1] != runValues[1] {
		t.Fatalf("expected only the two owned values: names=%v error=%v", names, err)
	}
	values[runValues[0]] = registryValue{exists: true, stringValue: `"` + filepath.Join(filepath.Dir(old), "other", launcherFile) + `" --silent`}
	values[runValues[1]] = registryValue{}
	names, err = prepareRunMigration(old, target, func() (map[string]registryValue, error) { return values, nil })
	if err != nil || len(names) != 0 {
		t.Fatalf("foreign or absent values must not require Run write access: names=%v error=%v", names, err)
	}
}

func TestRunRollbackIncludesOnlySuccessfulOwnedChanges(t *testing.T) {
	before := registryValue{exists: true, stringValue: `"C:\old\CodexMultiKey.exe" --silent`, stringType: 2}
	after := registryValue{exists: true, stringValue: `"C:\new\CodexMultiKey.exe" --silent`, stringType: 1}
	changes := map[string]runChange{}
	if names := changedRunNames(changes); len(names) != 0 {
		t.Fatalf("rollback would touch Run without a completed mutation: %v", names)
	}
	changes[runValues[0]] = runChange{before: before, after: after}
	if names := changedRunNames(changes); len(names) != 1 || names[0] != runValues[0] {
		t.Fatalf("rollback must touch only the modified value: %v", names)
	}
	if !runRollbackAllowed(changes[runValues[0]], after) {
		t.Fatal("rollback should restore the value left by this installer")
	}
	if runRollbackAllowed(changes[runValues[0]], registryValue{exists: true, stringValue: `"C:\other\app.exe"`}) {
		t.Fatal("rollback must preserve a value changed by another application")
	}
	if runRollbackAllowed(changes[runValues[0]], registryValue{exists: true, stringValue: after.stringValue, stringType: 2}) {
		t.Fatal("rollback must preserve a value whose type changed externally")
	}
}
