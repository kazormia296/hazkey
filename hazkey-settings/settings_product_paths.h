#ifndef GRIMODEX_SETTINGS_PRODUCT_PATHS_H_
#define GRIMODEX_SETTINGS_PRODUCT_PATHS_H_

#include <string>
#include <string_view>

#include "grimodex_product_identity.h"

namespace grimodex::ime::settings {

struct SettingsEnvironment {
    const char* runtimeHome{nullptr};
    const char* configHome{nullptr};
    const char* dataHome{nullptr};
    const char* stateHome{nullptr};
    const char* cacheHome{nullptr};
};

struct SettingsProductPaths {
    std::string runtimeSocket;
    std::string configDirectory;
    std::string dataDirectory;
    std::string stateDirectory;
    std::string cacheDirectory;
    std::string zenzaiModel;
};

inline bool hasPath(const char* value) {
    return value != nullptr && value[0] != '\0';
}

inline std::string productDirectory(const char* configuredHome,
                                    std::string_view homeDirectory,
                                    std::string_view fallbackSuffix) {
    const std::string base =
        hasPath(configuredHome)
            ? std::string(configuredHome)
            : appendPath(std::string(homeDirectory), fallbackSuffix);
    return appendPath(base, kPackageName);
}

inline SettingsProductPaths resolveSettingsProductPaths(
    const SettingsEnvironment& environment, std::string_view homeDirectory,
    unsigned int uid) {
    const auto runtime = resolveRuntimePaths(environment.runtimeHome, uid);
    const auto config = productDirectory(environment.configHome, homeDirectory,
                                         ".config");
    const auto data = productDirectory(environment.dataHome, homeDirectory,
                                       ".local/share");
    const auto state = productDirectory(environment.stateHome, homeDirectory,
                                        ".local/state");
    const auto cache = productDirectory(environment.cacheHome, homeDirectory,
                                        ".cache");
    return SettingsProductPaths{
        .runtimeSocket = runtime.socket,
        .configDirectory = config,
        .dataDirectory = data,
        .stateDirectory = state,
        .cacheDirectory = cache,
        .zenzaiModel = appendPath(appendPath(data, "zenzai"), "zenzai.gguf"),
    };
}

}  // namespace grimodex::ime::settings

#endif  // GRIMODEX_SETTINGS_PRODUCT_PATHS_H_
