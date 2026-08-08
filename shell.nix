{ pkgs ? import <nixpkgs> { } }:

let
  # The generated client, taken from PyPI like any other dependency. It is not
  # in nixpkgs, so it is built here rather than resolved from a checkout: there
  # is one source of truth for what `yazio_sdk` means, and it is the release.
  #
  # Keep the version inside the range pyproject.toml declares. To move it, bump
  # `version` and replace `hash` with the sha256 nix reports on the first build.
  yazio-sdk = pkgs.python3Packages.buildPythonPackage rec {
    pname = "yazio_sdk";
    version = "0.4.0";
    pyproject = true;

    src = pkgs.python3Packages.fetchPypi {
      inherit pname version;
      sha256 = "fb2d5ecf9342f0d647ced80a85d64fc4e63644e21c06a6b7b1e245d4fa737c9a";
    };

    build-system = [ pkgs.python3Packages.hatchling ];

    dependencies = with pkgs.python3Packages; [
      attrs
      httpx
      python-dateutil
    ];

    # The sdist ships the generated client only; there is no test suite to run.
    doCheck = false;

    pythonImportsCheck = [ "yazio_sdk" ];
  };

  python = pkgs.python3.withPackages (ps: [
    # Runtime dependencies of the server itself.
    ps.mcp # FastMCP server + streamable-HTTP transport
    ps.starlette # the ASGI middleware the Basic-auth layer hooks into
    ps.uvicorn # ASGI server used by yazio_mcp.__main__
    yazio-sdk # the generated YAZIO client, built from PyPI above

    ps.pytest
    ps.pytest-asyncio
    ps.respx # httpx mock transport, used to fake the YAZIO API
  ]);
in
pkgs.mkShell {
  name = "yazio-mcp-server";

  packages = [
    python
    pkgs.ruff
  ];

  shellHook = ''
    export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"

    echo "yazio-mcp-server dev shell"
    echo "  yazio_sdk    ${yazio-sdk.version} (from PyPI)"
    echo
    echo "  make run     start the server on 127.0.0.1:8931"
    echo "  make test    run the mocked test suite"
  '';
}
