#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
PLUGINS_DIR="$HOME/.config/omarchy/plugins"
CONFIG_DIR="$HOME/.config/castanha"

echo "🌰 Instalando Castanha para Omarchy/Linux..."

mkdir -p "$BIN_DIR"
mkdir -p "$PLUGINS_DIR"
mkdir -p "$CONFIG_DIR"
mkdir -p "$HOME/Notes/Meetings/bronze"
mkdir -p "$HOME/Notes/Meetings/silver"
mkdir -p "$HOME/Notes/Meetings/gold"

# 1. Symlink CLI
ln -sf "$PROJECT_DIR/bin/castanha" "$BIN_DIR/castanha"
chmod +x "$BIN_DIR/castanha"
echo "  ✔ CLI instalado em $BIN_DIR/castanha"

# 2. Symlink Omarchy Plugin
rm -rf "$PLUGINS_DIR/moniz.castanha"
ln -sf "$PROJECT_DIR/plugin" "$PLUGINS_DIR/moniz.castanha"
echo "  ✔ Plugin do Omarchy linkado em $PLUGINS_DIR/moniz.castanha"

# 3. Configuração padrão
if [ ! -f "$CONFIG_DIR/config.json" ]; then
    cp "$PROJECT_DIR/config.example.json" "$CONFIG_DIR/config.json"
    echo "  ✔ Arquivo de configuração criado em $CONFIG_DIR/config.json"
fi

echo ""
echo "🎉 Castanha instalado com sucesso!"
echo ""
echo "Para recarregar o plugin no Omarchy:"
echo "  omarchy-shell shell rescanPlugins"
echo ""
echo "Para adicionar o atalho global no Hyprland (~/.config/hypr/hyprland.conf):"
echo "  bind = \$mainMod ALT, R, exec, castanha toggle"
echo ""
