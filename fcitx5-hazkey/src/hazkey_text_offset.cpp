#include "hazkey_text_offset.h"

#include <fcitx-utils/utf8.h>

#include <cstdint>
#include <limits>

namespace fcitx {

std::optional<std::int32_t> hazkeyAnchorAfterAppend(
    std::int64_t currentAnchor, std::string_view appendText) {
    constexpr auto maxAnchor = std::numeric_limits<std::int32_t>::max();
    if (currentAnchor < 0 || currentAnchor > maxAnchor) {
        return std::nullopt;
    }

    const auto appendLength = appendText.empty()
                                  ? std::size_t{0}
                                  : utf8::lengthValidated(appendText);
    if (appendLength == utf8::INVALID_LENGTH ||
        appendLength >
            static_cast<std::size_t>(maxAnchor - currentAnchor)) {
        return std::nullopt;
    }

    return static_cast<std::int32_t>(currentAnchor + appendLength);
}

}  // namespace fcitx
