#ifndef _FCITX5_HAZKEY_HAZKEY_TEXT_OFFSET_H_
#define _FCITX5_HAZKEY_HAZKEY_TEXT_OFFSET_H_

#include <cstdint>
#include <optional>
#include <string_view>

namespace fcitx {

/// Advance an Fcitx surrounding-text anchor by valid UTF-8 text.
///
/// Fcitx anchors and Grimodex's SetContext protocol both count Unicode scalar
/// values, while std::string::size() counts UTF-8 bytes.
std::optional<std::int32_t> hazkeyAnchorAfterAppend(
    std::int64_t currentAnchor, std::string_view appendText);

}  // namespace fcitx

#endif  // _FCITX5_HAZKEY_HAZKEY_TEXT_OFFSET_H_
