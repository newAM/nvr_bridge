{
  lib,
  python3,
  python3Packages,
  src,
}: let
  pyprojectToml = lib.importTOML (src + "/pyproject.toml");
in
  python3Packages.buildPythonApplication (finalAttrs: {
    pname = pyprojectToml.project.name;
    version = pyprojectToml.project.version;
    pyproject = true;

    inherit src;

    build-system = with python3Packages; [
      setuptools
    ];

    dependencies = with python3Packages; [
      authlib
      flask
      flask-session
      flask-sqlalchemy
      flask-wtf
      gunicorn
      requests
      systemd-python
      (python3Packages.callPackage ../lib/package.nix {})
    ];

    nativeCheckInputs = with python3Packages; [
      pytestCheckHook
      ty
    ];

    postCheck = ''
      ty check .
    '';

    pythonImportsCheck = [
      "nvr_core"
    ];

    postInstall = let
      pythonPath = python3Packages.makePythonPath finalAttrs.passthru.dependencies;
    in ''
      makeWrapper ${lib.getExe python3Packages.gunicorn} $out/bin/gunicorn \
        --prefix PYTHONPATH : "${pythonPath}:$out/${python3.sitePackages}"
    '';

    meta = {
      description = pyprojectToml.project.description;
      homepage = pyprojectToml.project.urls.Repository;
      license = lib.licenses.mit;
      mainProgram = "gunicorn";
      maintainers = [lib.maintainers.newam];
    };
  })
