#include "hazkey_text_offset.h"

#include <cstdlib>
#include <iostream>
#include <string>
#include <string_view>

namespace {

void expect(bool condition, std::string_view message) {
    if (!condition) {
        std::cerr << "FAIL: " << message << '\n';
        std::exit(EXIT_FAILURE);
    }
}

void expectContext(std::string_view surroundingText, std::size_t fcitxAnchor,
                   std::string_view appendText, std::string_view expectedText,
                   std::size_t expectedSwiftAnchor) {
    const auto result = fcitx::makeHazkeySurroundingContext(
        surroundingText, fcitxAnchor, appendText);

    expect(result.has_value(), "valid UTF-8 context must be converted");
    expect(result->text == expectedText, "text must be inserted at the anchor");
    expect(result->anchor == expectedSwiftAnchor,
           "anchor must use Swift Character offsets");
}

}  // namespace

int main() {
    expectContext("leftright", 4, "word", "leftwordright", 8);
    expectContext("\xE5\x89\x8D\xE5\xBE\x8C", 1,
                  "\xE4\xB8\x96\xE7\x95\x8C",
                  "\xE5\x89\x8D\xE4\xB8\x96\xE7\x95\x8C\xE5\xBE\x8C", 3);

    // Swift treats a base letter plus a combining mark as one Character.
    expectContext("x", 1, "e\xCC\x81", "xe\xCC\x81", 2);
    expectContext("e\xCC\x81x", 2, "!", "e\xCC\x81!x", 2);

    // A family joined with ZWJ is also one Swift Character.
    const std::string family =
        "\xF0\x9F\x91\xA8\xE2\x80\x8D\xF0\x9F\x91\xA9\xE2\x80\x8D"
        "\xF0\x9F\x91\xA7\xE2\x80\x8D\xF0\x9F\x91\xA6";
    expectContext("x", 1, family, "x" + family, 2);
    expectContext(family + "x", 7, "!", family + "!x", 2);

    expect(!fcitx::makeHazkeySurroundingContext("text", 5, "x").has_value(),
           "out-of-range Fcitx anchor must fail closed");
    expect(!fcitx::makeHazkeySurroundingContext("\xFF", 0, "x").has_value(),
           "invalid surrounding UTF-8 must fail closed");
    expect(!fcitx::makeHazkeySurroundingContext("text", 4, "\xFF").has_value(),
           "invalid appended UTF-8 must fail closed");

    std::cout << "hazkey text offset tests passed\n";
    return EXIT_SUCCESS;
}
