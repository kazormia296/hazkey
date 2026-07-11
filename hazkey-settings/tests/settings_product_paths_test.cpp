#include <cstdlib>
#include <iostream>
#include <string_view>

#include "settings_product_paths.h"

namespace {

void expect(bool condition, std::string_view message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::exit(1);
    }
}

}  // namespace

int main() {
    using grimodex::ime::settings::SettingsEnvironment;
    using grimodex::ime::settings::resolveSettingsProductPaths;

    const SettingsEnvironment xdg{
        .runtimeHome = "/run/user/1000",
        .configHome = "/xdg/config",
        .dataHome = "/xdg/data",
        .stateHome = "/xdg/state",
        .cacheHome = "/xdg/cache",
    };
    const auto paths =
        resolveSettingsProductPaths(xdg, "/home/writer", 1000);

    expect(paths.runtimeSocket ==
               "/run/user/1000/fcitx5-grimodex/server.sock",
           "settings must connect to the product-scoped runtime socket");
    expect(paths.configDirectory == "/xdg/config/fcitx5-grimodex",
           "settings config must not share Hazkey state");
    expect(paths.dataDirectory == "/xdg/data/fcitx5-grimodex",
           "settings data must not share Hazkey state");
    expect(paths.stateDirectory == "/xdg/state/fcitx5-grimodex",
           "settings state must not share Hazkey state");
    expect(paths.cacheDirectory == "/xdg/cache/fcitx5-grimodex",
           "settings cache must not share Hazkey state");
    expect(paths.zenzaiModel ==
               "/xdg/data/fcitx5-grimodex/zenzai/zenzai.gguf",
           "local Zenzai model must live in the isolated data directory");

    const auto fallback =
        resolveSettingsProductPaths({}, "/home/writer", 42);
    expect(fallback.runtimeSocket ==
               "/tmp/fcitx5-grimodex-42/server.sock",
           "fallback runtime socket must match the server");
    expect(fallback.configDirectory ==
               "/home/writer/.config/fcitx5-grimodex",
           "fallback config must be product-scoped");
    expect(fallback.dataDirectory ==
               "/home/writer/.local/share/fcitx5-grimodex",
           "fallback data must be product-scoped");
    expect(fallback.stateDirectory ==
               "/home/writer/.local/state/fcitx5-grimodex",
           "fallback state must be product-scoped");
    expect(fallback.cacheDirectory ==
               "/home/writer/.cache/fcitx5-grimodex",
           "fallback cache must be product-scoped");
}
