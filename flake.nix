{
  description = "Run Minecraft Bedrock for Windows (GDK) on Linux with native Xbox identity";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          bolPython = pkgs.python313.withPackages (ps: with ps; [
            tkinter
            cryptography
            customtkinter
            darkdetect
            packaging
            python-xlib
            certifi
          ]);
        in
        {
          default = pkgs.stdenv.mkDerivation {
            pname = "bedrock-on-linux";
            version = "2.1.3";

            src = ./.;

            nativeBuildInputs = [ pkgs.makeWrapper ];

            dontBuild = true;

            installPhase = ''
              runHook preInstall

              mkdir -p $out/lib/bedrock-on-linux $out/bin $out/share/applications $out/share/icons/hicolor/256x256/apps

              # Install the Python package
              cp -r bol $out/lib/bedrock-on-linux/
              cp bedrock-on-linux $out/lib/bedrock-on-linux/

              # Desktop entry and icon
              cp data/bedrock-on-linux.desktop $out/share/applications/
              cp data/icon.png $out/share/icons/hicolor/256x256/apps/bedrock-on-linux.png

              # Wrapper script
              makeWrapper ${bolPython}/bin/python3 $out/bin/bedrock-on-linux \
                --add-flags "$out/lib/bedrock-on-linux/bedrock-on-linux" \
                --prefix PYTHONPATH : "$out/lib/bedrock-on-linux"

              runHook postInstall
            '';

            meta = {
              description = "Run Minecraft Bedrock for Windows (GDK edition) on Linux";
              homepage = "https://github.com/Cephalode/BedrockOnLinux";
              license = pkgs.lib.licenses.mit;
              platforms = [ "x86_64-linux" ];
              mainProgram = "bedrock-on-linux";
            };
          };
        });
    };
}
