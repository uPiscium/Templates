root := justfile_directory()

mod cmake 'cmake.just'
mod quality 'quality.just'
mod test 'test.just'
mod? repository 'repository.just'

[working-directory: root]
doctor:
    @test -f CMakeLists.txt
    @command -v cmake >/dev/null
    @command -v ninja >/dev/null
    @command -v clang++ >/dev/null
    @command -v clang-format >/dev/null
    @command -v clang-tidy >/dev/null
    @command -v ctest >/dev/null
    @if [ -f CMakePresets.json ]; then cmake --list-presets >/dev/null; fi
    @echo "C++/CMake Project Adapter doctor: PASS"

configure: cmake::configure

format-check: quality::format-check

lint: quality::lint

test: test::all

build: cmake::build

check: doctor configure format-check lint test build
    @echo "C++/CMake Project Adapter check: PASS"
