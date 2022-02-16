from homcc.server.environment import *


class TestServerEnvironment:
    def test_map_arguments(self):
        instance_path = "/client1"
        mapped_cwd = "/client1/test/xyz"

        arguments = [
            "gcc",
            "-Irelative_path/relative.h",
            "-I/var/includes/absolute.h",
            "main.cpp",
            "relative/relative.cpp",
            "/opt/src/absolute.cpp",
        ]

        mapped_arguments = map_arguments(instance_path, mapped_cwd, arguments)

        assert mapped_arguments[0] == "gcc"
        assert mapped_arguments[1] == f"-I{mapped_cwd}/relative_path/relative.h"
        assert mapped_arguments[2] == f"-I{instance_path}/var/includes/absolute.h"
        assert mapped_arguments[3] == f"{mapped_cwd}/main.cpp"
        assert mapped_arguments[4] == f"{mapped_cwd}/relative/relative.cpp"
        assert mapped_arguments[5] == f"{instance_path}/opt/src/absolute.cpp"

    def test_map_cwd(self):
        instance_path = "/client1/"
        cwd = "/home/xyz/query-engine"

        mapped_cwd = map_cwd(instance_path, cwd)

        assert mapped_cwd == "/client1/home/xyz/query-engine"

    def test_extract_source_files(self):
        source_file_arguments = [
            "main.cpp",
            "relative/relative.cpp",
            "/opt/src/absolute.cpp",
        ]
        arguments = [
            "gcc",
            "-Irelative_path/relative.h",
            "-I/var/includes/absolute.h",
        ] + source_file_arguments

        assert extract_source_files(arguments) == source_file_arguments
