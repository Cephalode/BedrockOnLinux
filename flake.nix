{
  description = "Run Minecraft Bedrock for Windows (GDK) on Linux with native Xbox identity";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      pkgs = nixpkgs.legacyPackages.x86_64-linux;
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
      packages.x86_64-linux.default = pkgs.stdenv.mkDerivation {
        pname = "bedrock-on-linux";
        version = "2.1.3";

        src = ./.;

        nativeBuildInputs = [ pkgs.makeWrapper ];

        installPhase = ''
          mkdir -p $out/lib/bedrock-on-linux $out/bin $out/share/applications $out/share/icons/hicolor/256x256/apps

          cp -r bol $out/lib/bedrock-on-linux/
          cp bedrock-on-linux $out/lib/bedrock-on-linux/

          cp data/bedrock-on-linux.desktop $out/share/applications/
          cp data/icon.png $out/share/icons/hicolor/256x256/apps/bedrock-on-linux.png

          makeWrapper ${bolPython}/bin/python3 $out/bin/bedrock-on-linux \
            --add-flags "$out/lib/bedrock-on-linux/bedrock-on-linux" \
            --prefix PYTHONPATH : "$out/lib/bedrock-on-linux"
        '';

        meta = {
          homepage = "https://github.com/Wyze3306/BedrockOnLinux";
          license = pkgs.lib.licenses.mit;
          platforms = [ "x86_64-linux" ];
          mainProgram = "bedrock-on-linux";
        };
      };
    };
}
