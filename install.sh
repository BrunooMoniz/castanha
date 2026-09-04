#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="$HOME/.config/omarchy/plugins"
PLUGIN_ID="io.github.brunoomoniz.castanha"

# 1. Roda setup de dependências, CLI e configuração
"$PROJECT_DIR/setup"

# 2. Symlink do Plugin no Omarchy
mkdir -p "$PLUGINS_DIR"
rm -rf "$PLUGINS_DIR/moniz.castanha"
rm -rf "$PLUGINS_DIR/$PLUGIN_ID"
ln -sf "$PROJECT_DIR" "$PLUGINS_DIR/$PLUGIN_ID"
echo "  ✔ Plugin do Omarchy linkado em $PLUGINS_DIR/$PLUGIN_ID"

echo ""
echo "Para recarregar o plugin no Omarchy:"
echo "  omarchy-shell shell rescanPlugins"
echo "  omarchy bar put $PLUGIN_ID"
echo ""
echo "Para adicionar o atalho global no Hyprland (~/.config/hypr/hyprland.conf):"
echo "  bind = \$mainMod ALT, R, exec, castanha toggle"
echo ""
