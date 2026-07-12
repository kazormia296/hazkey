#ifndef GRIMODEX_PRODUCT_IDENTITY_H
#define GRIMODEX_PRODUCT_IDENTITY_H

#include <string>
#include <string_view>

namespace grimodex::ime {

inline constexpr std::string_view kPackageName = "fcitx5-grimodex";
inline constexpr std::string_view kAddonId = "grimodex";
inline constexpr std::string_view kInputMethodId = "grimodex";
inline constexpr std::string_view kServerExecutable = "fcitx5-grimodex-server";
inline constexpr std::string_view kSettingsExecutable = "fcitx5-grimodex-settings";
inline constexpr std::string_view kRuntimeDirectoryName = "fcitx5-grimodex";

struct RuntimePaths {
    std::string directory;
    std::string socket;
    std::string lock;
};

inline std::string appendPath(std::string base, std::string_view component) {
    while (base.size() > 1 && base.back() == '/') {
        base.pop_back();
    }
    base.push_back('/');
    base.append(component);
    return base;
}

inline RuntimePaths resolveRuntimePaths(const char* xdgRuntimeDirectory,
                                        unsigned int uid) {
    std::string directory;
    if (xdgRuntimeDirectory != nullptr && xdgRuntimeDirectory[0] != '\0') {
        directory = appendPath(xdgRuntimeDirectory, kRuntimeDirectoryName);
    } else {
        directory = "/tmp/" + std::string(kRuntimeDirectoryName) + "-" +
                    std::to_string(uid);
    }
    return RuntimePaths{
        .directory = directory,
        .socket = appendPath(directory, "server.sock"),
        .lock = appendPath(directory, "server.lock"),
    };
}

}  // namespace grimodex::ime

#endif  // GRIMODEX_PRODUCT_IDENTITY_H
