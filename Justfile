set minimum-version := "1.31.0"

mod template 'just/template.just'
mod runtime 'just/runtime.just'

default:
    @just --list
