#include "hazkey_text_offset.h"

#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <string_view>

namespace {

void expect(bool condition, std::string_view message) {
    if (!condition) {
        std::cerr << "FAIL: " << message << '\n';
        std::exit(EXIT_FAILURE);
    }
}

void expectAnchor(std::int32_t currentAnchor, std::string_view appendText,
                  std::int32_t expectedAnchor) {
    const auto result =
        fcitx::hazkeyAnchorAfterAppend(currentAnchor, appendText);

    expect(result.has_value(), "valid UTF-8 append text must be counted");
    expect(*result == expectedAnchor,
           "anchor must use Fcitx Unicode scalar offsets");
}

}  // namespace

int main() {
    expectAnchor(4, "word", 8);
    expectAnchor(1, "\xE4\xB8\x96\xE7\x95\x8C", 3);

    // Fcitx surrounding-text offsets count Unicode scalars, not graphemes.
    expectAnchor(1, "e\xCC\x81", 3);

    // Four emoji joined with three ZWJs occupy seven scalar offsets.
    constexpr std::string_view family =
        "\xF0\x9F\x91\xA8\xE2\x80\x8D\xF0\x9F\x91\xA9\xE2\x80\x8D"
        "\xF0\x9F\x91\xA7\xE2\x80\x8D\xF0\x9F\x91\xA6";
    expectAnchor(1, family, 8);
    expectAnchor(7, "", 7);

    expect(!fcitx::hazkeyAnchorAfterAppend(4, "\xFF").has_value(),
           "invalid appended UTF-8 must fail closed");
    expect(!fcitx::hazkeyAnchorAfterAppend(-1, "x").has_value(),
           "negative anchors must fail closed");
    expect(!fcitx::hazkeyAnchorAfterAppend(
                std::numeric_limits<std::int32_t>::max(), "x")
                .has_value(),
           "anchor overflow must fail closed");

    std::cout << "hazkey text offset tests passed\n";
    return EXIT_SUCCESS;
}
