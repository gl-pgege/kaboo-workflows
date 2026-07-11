# https://just.systems/man/en/

# SETTINGS

set dotenv-load := true
set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# VARIABLES

PACKAGE    := "kaboo-workflows"
SOURCES    := "src/kaboo_workflows"
TESTS      := "tests"
EXAMPLES   := "examples"

# DEFAULTS

# display help information
default:
    @just --list

# IMPORTS

import 'tasks/check.just'
import 'tasks/clean.just'
import 'tasks/commit.just'
import 'tasks/docs.just'
import 'tasks/format.just'
import 'tasks/install.just'
import 'tasks/release.just'
import 'tasks/test.just'
