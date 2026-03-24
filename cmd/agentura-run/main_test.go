package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestLoadDotenv(t *testing.T) {
	// Create temp .env file
	dir := t.TempDir()
	envFile := filepath.Join(dir, ".env")
	os.WriteFile(envFile, []byte(
		"FOO=bar\n"+
			"# comment\n"+
			"\n"+
			"BAZ=qux\n"+
			"  SPACED = value  \n",
	), 0644)

	// Change to temp dir and load
	origDir, _ := os.Getwd()
	os.Chdir(dir)
	defer os.Chdir(origDir)

	// Clear any existing values
	os.Unsetenv("FOO")
	os.Unsetenv("BAZ")
	os.Unsetenv("SPACED")

	loadDotenv()

	if v := os.Getenv("FOO"); v != "bar" {
		t.Errorf("FOO: got %q, want %q", v, "bar")
	}
	if v := os.Getenv("BAZ"); v != "qux" {
		t.Errorf("BAZ: got %q, want %q", v, "qux")
	}
	if v := os.Getenv("SPACED"); v != "value" {
		t.Errorf("SPACED: got %q, want %q", v, "value")
	}
}

func TestLoadDotenvSetdefault(t *testing.T) {
	dir := t.TempDir()
	envFile := filepath.Join(dir, ".env")
	os.WriteFile(envFile, []byte("EXISTING=from_env\n"), 0644)

	origDir, _ := os.Getwd()
	os.Chdir(dir)
	defer os.Chdir(origDir)

	// Pre-set EXISTING — dotenv should NOT overwrite
	os.Setenv("EXISTING", "original")
	defer os.Unsetenv("EXISTING")

	loadDotenv()

	if v := os.Getenv("EXISTING"); v != "original" {
		t.Errorf("setdefault broken: got %q, want %q", v, "original")
	}
}

func TestLoadDotenvMissing(t *testing.T) {
	dir := t.TempDir()
	origDir, _ := os.Getwd()
	os.Chdir(dir)
	defer os.Chdir(origDir)

	// Should not panic when .env doesn't exist
	loadDotenv()
}

func TestLoadDotenvComments(t *testing.T) {
	dir := t.TempDir()
	envFile := filepath.Join(dir, ".env")
	os.WriteFile(envFile, []byte(
		"# This is a comment\n"+
			"KEY=val\n"+
			"  # Indented comment\n",
	), 0644)

	origDir, _ := os.Getwd()
	os.Chdir(dir)
	defer os.Chdir(origDir)

	os.Unsetenv("KEY")
	loadDotenv()

	if v := os.Getenv("KEY"); v != "val" {
		t.Errorf("KEY: got %q, want %q", v, "val")
	}
}

func TestEnsureClaudeTrust(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, ".claude.json")

	// Override home dir for test
	origHome := os.Getenv("HOME")
	os.Setenv("HOME", dir)
	defer os.Setenv("HOME", origHome)

	ensureClaudeTrust("/test/cwd")

	// Read and verify
	data, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("config not written: %v", err)
	}

	var config map[string]interface{}
	json.Unmarshal(data, &config)

	projects, _ := config["projects"].(map[string]interface{})
	if projects == nil {
		t.Fatal("projects key missing")
	}
	project, _ := projects["/test/cwd"].(map[string]interface{})
	if project == nil {
		t.Fatal("project entry missing")
	}
	if trusted, _ := project["hasTrustDialogAccepted"].(bool); !trusted {
		t.Error("hasTrustDialogAccepted should be true")
	}
}

func TestEnsureClaudeTrustIdempotent(t *testing.T) {
	dir := t.TempDir()

	origHome := os.Getenv("HOME")
	os.Setenv("HOME", dir)
	defer os.Setenv("HOME", origHome)

	// Pre-create config with existing data
	existing := map[string]interface{}{
		"theme": "dark",
		"projects": map[string]interface{}{
			"/existing": map[string]interface{}{
				"hasTrustDialogAccepted": true,
			},
		},
	}
	data, _ := json.MarshalIndent(existing, "", "  ")
	os.WriteFile(filepath.Join(dir, ".claude.json"), data, 0644)

	// Trust a new directory
	ensureClaudeTrust("/new/cwd")

	// Verify existing data preserved
	raw, _ := os.ReadFile(filepath.Join(dir, ".claude.json"))
	var config map[string]interface{}
	json.Unmarshal(raw, &config)

	if config["theme"] != "dark" {
		t.Error("existing data lost")
	}
	projects, _ := config["projects"].(map[string]interface{})
	if _, ok := projects["/existing"]; !ok {
		t.Error("existing project lost")
	}
	if _, ok := projects["/new/cwd"]; !ok {
		t.Error("new project not added")
	}
}
