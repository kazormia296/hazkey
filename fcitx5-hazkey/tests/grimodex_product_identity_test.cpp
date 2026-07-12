#include <cstdlib>
#include <iostream>
#include <string>
#include <string_view>

#include "grimodex_product_identity.h"

namespace {

void expect(bool condition, std::string_view message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::exit(1);
    }
}

}  // namespace

int main() {
    using namespace grimodex::ime;

    expect(kPackageName == "fcitx5-grimodex", "package identity must be independent");
    expect(kAddonId == "grimodex", "Fcitx addon ID must be independent");
    expect(kInputMethodId == "grimodex", "Fcitx input method ID must be independent");
    expect(kServerExecutable == "fcitx5-grimodex-server",
           "server executable must be independent");
    expect(kSettingsExecutable == "fcitx5-grimodex-settings",
           "settings executable must be independent");

    const auto xdg = resolveRuntimePaths("/run/user/1000", 1000);
    expect(xdg.directory == "/run/user/1000/fcitx5-grimodex",
           "XDG runtime directory must be product-scoped");
    expect(xdg.socket == "/run/user/1000/fcitx5-grimodex/server.sock",
           "XDG socket must be product-scoped");
    expect(xdg.lock == "/run/user/1000/fcitx5-grimodex/server.lock",
           "XDG lock must be product-scoped");

    const auto fallback = resolveRuntimePaths(nullptr, 1000);
    expect(fallback.directory == "/tmp/fcitx5-grimodex-1000",
           "fallback runtime directory must be user- and product-scoped");
    expect(fallback.socket == "/tmp/fcitx5-grimodex-1000/server.sock",
           "fallback socket must match the server");
    expect(fallback.lock == "/tmp/fcitx5-grimodex-1000/server.lock",
           "fallback lock must match the server");
}
