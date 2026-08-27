set minimum-version := "1.55.0"

mod template 'just/template.just'
mod runtime 'just/runtime.just'
mod agent-core 'just/agent-core.just'

default:
    @just --list
