{
  lib,
  buildPythonPackage,
  setuptools,
  pytestCheckHook,
  ty,
}: let
  pyprojectToml = lib.importTOML ./pyproject.toml;
in
  buildPythonPackage (finalAttrs: {
    pname = pyprojectToml.project.name;
    version = pyprojectToml.project.version;
    pyproject = true;

    src = lib.fileset.toSource {
      root = ./.;
      fileset = lib.fileset.unions [
        ./nvr_common
        ./pyproject.toml
        ./tests
      ];
    };

    build-system = [
      setuptools
    ];

    dependencies = [];

    nativeCheckInputs = [
      pytestCheckHook
      ty
    ];

    postCheck = ''
      ty check .
    '';

    meta = {
      description = pyprojectToml.project.description;
      homepage = pyprojectToml.project.urls.Repository;
      license = lib.licenses.mit;
      maintainers = [lib.maintainers.newam];
    };
  })
