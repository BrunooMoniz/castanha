#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="$HOME/.config/omarchy/plugins"
PLUGIN_ID="io.github.brunoomoniz.castanha"

castanha_lang() {
    case "${LC_ALL:-${LANG:-}}" in
        [pP][tT]*) echo "pt" ;;
        *) echo "en" ;;
    esac
}

t() {
    if [ "$(castanha_lang)" = "pt" ]; then
        case "$1" in
            plugin_linked) echo "  ✔ Plugin do Omarchy linkado em $2" ;;
            reload_hint) echo "Para recarregar o plugin no Omarchy:" ;;
            shortcut_hint) echo "Para adicionar o atalho global no Hyprland (~/.config/hypr/hyprland.conf):" ;;
        esac
    else
        case "$1" in
            plugin_linked) echo "  ✔ Omarchy plugin linked at $2" ;;
            reload_hint) echo "To reload the plugin in Omarchy:" ;;
            shortcut_hint) echo "To add the global shortcut in Hyprland (~/.config/hypr/hyprland.conf):" ;;
        esac
    fi
}

# 1. Roda setup de dependências, CLI e configuração
"$PROJECT_DIR/setup"

# 2. Symlink do Plugin no Omarchy
mkdir -p "$PLUGINS_DIR"
rm -rf "$PLUGINS_DIR/moniz.castanha"
rm -rf "$PLUGINS_DIR/$PLUGIN_ID"
ln -sf "$PROJECT_DIR" "$PLUGINS_DIR/$PLUGIN_ID"
t plugin_linked "$PLUGINS_DIR/$PLUGIN_ID"

echo ""
t reload_hint
echo "  omarchy-shell shell rescanPlugins"
echo "  omarchy bar put $PLUGIN_ID"
echo ""
t shortcut_hint
echo "  bind = \$mainMod ALT, R, exec, castanha toggle"
echo ""
