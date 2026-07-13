#include <cstdlib>
#include <iostream>
#include <string>

#include <fcitx-utils/textformatflags.h>

#include "base.pb.h"
#include "hazkey_snapshot_renderer.h"

namespace {

[[noreturn]] void fail(const std::string& message) {
    std::cerr << message << '\n';
    std::exit(1);
}

void expect(bool condition, const std::string& message) {
    if (!condition) {
        fail(message);
    }
}

void rendersUnicodeSpansAndByteCaretExactly() {
    hazkey::SessionSnapshot snapshot;
    auto* active = snapshot.add_preedit();
    active->set_text("変換");
    active->set_style(hazkey::PreeditSpan::ACTIVE);
    auto* suffix = snapshot.add_preedit();
    suffix->set_text("𠮷は\u3099👨‍👩‍👧‍👦");
    suffix->set_style(hazkey::PreeditSpan::UNDERLINE);
    snapshot.set_caret_utf8_byte_offset(
        static_cast<uint32_t>(active->text().size()));

    const auto rendered = fcitx::HazkeySnapshotRenderer::renderPreedit(snapshot);
    expect(rendered.toString() == active->text() + suffix->text(),
           "renderer must preserve every UTF-8 byte and grapheme");
    expect(rendered.cursor() == static_cast<int>(active->text().size()),
           "caret must use the protocol UTF-8 byte offset");
    expect(rendered.size() == 2, "span boundaries must be preserved");
    expect(rendered.formatAt(0).test(fcitx::TextFormatFlag::HighLight),
           "active segment must be highlighted");
    expect(rendered.formatAt(1).test(fcitx::TextFormatFlag::Underline),
           "unfocused suffix must be underlined");
    expect(
        fcitx::HazkeySnapshotRenderer::utf8ByteLength(snapshot) ==
            active->text().size() + suffix->text().size(),
        "reported preedit length must be a UTF-8 byte length");
}

void clampsMalformedCaretOffsetsWithoutChangingText() {
    hazkey::SessionSnapshot snapshot;
    snapshot.add_preedit()->set_text("𠮷");
    snapshot.set_caret_utf8_byte_offset(999);

    const auto rendered = fcitx::HazkeySnapshotRenderer::renderPreedit(snapshot);
    expect(rendered.cursor() == static_cast<int>(std::string("𠮷").size()),
           "malformed remote caret offsets must clamp to the preedit end");
    expect(rendered.toString() == "𠮷", "caret clamping must not alter text");
}

void canAnchorTheInputPanelAtThePreeditStart() {
    hazkey::SessionSnapshot snapshot;
    snapshot.add_preedit()->set_text("変換");
    snapshot.set_caret_utf8_byte_offset(
        static_cast<uint32_t>(std::string("変換").size()));

    const auto rendered = fcitx::HazkeySnapshotRenderer::renderPreedit(
        snapshot, true);
    expect(rendered.cursor() == 0,
           "the optional fixed cursor must anchor the input panel at the preedit start");
    expect(rendered.toString() == "変換",
           "the fixed cursor must not change the preedit text");
}

void rendersDisplayOnlyBoundaryForTheActiveSegment() {
    hazkey::SessionSnapshot snapshot;
    snapshot.set_phase(hazkey::SELECTING);
    auto* active = snapshot.add_preedit();
    active->set_text("東京");
    active->set_style(hazkey::PreeditSpan::ACTIVE);
    auto* remaining = snapshot.add_preedit();
    remaining->set_text("に行く");
    remaining->set_style(hazkey::PreeditSpan::UNDERLINE);
    snapshot.set_caret_utf8_byte_offset(
        static_cast<uint32_t>(active->text().size() + remaining->text().size()));

    const auto rendered = fcitx::HazkeySnapshotRenderer::renderPreedit(snapshot);
    expect(rendered.toString() == "東京│に行く",
           "segment editing must display a visible boundary");
    expect(rendered.toStringForCommit() == "東京に行く",
           "the segment boundary must never be committed");
    expect(rendered.formatAt(1).test(fcitx::TextFormatFlag::DontCommit),
           "the segment boundary must be marked as display-only");
    expect(rendered.cursor() == static_cast<int>(rendered.toString().size()),
           "a caret after the boundary must account for its display-only bytes");
}

void rendersEveryAdjacentConversionSegmentBoundary() {
    hazkey::SessionSnapshot snapshot;
    snapshot.set_phase(hazkey::PREVIEWING);
    auto* first = snapshot.add_preedit();
    first->set_text("東京");
    first->set_style(hazkey::PreeditSpan::UNDERLINE);
    auto* active = snapshot.add_preedit();
    active->set_text("都へ");
    active->set_style(hazkey::PreeditSpan::ACTIVE);
    auto* last = snapshot.add_preedit();
    last->set_text("行く");
    last->set_style(hazkey::PreeditSpan::UNDERLINE);
    snapshot.set_caret_utf8_byte_offset(static_cast<uint32_t>(
        first->text().size() + active->text().size()));

    const auto rendered = fcitx::HazkeySnapshotRenderer::renderPreedit(snapshot);
    expect(rendered.toString() == "東京│都へ│行く",
           "every adjacent conversion segment must have a visible boundary");
    expect(rendered.toStringForCommit() == "東京都へ行く",
           "no conversion segment boundary may enter committed text");
    expect(rendered.cursor() == static_cast<int>(
        first->text().size() + std::string("│").size() + active->text().size()),
           "the active-middle caret must account only for preceding markers");
    expect(rendered.cursor() != -1,
           "non-empty conversion preedit must publish an explicit caret");
}

void doesNotRenderSegmentBoundariesWhileComposing() {
    hazkey::SessionSnapshot snapshot;
    snapshot.set_phase(hazkey::COMPOSING);
    auto* live = snapshot.add_preedit();
    live->set_text("東京");
    live->set_style(hazkey::PreeditSpan::ACTIVE);
    auto* suffix = snapshot.add_preedit();
    suffix->set_text("と");
    suffix->set_style(hazkey::PreeditSpan::UNDERLINE);

    const auto rendered = fcitx::HazkeySnapshotRenderer::renderPreedit(snapshot);
    expect(rendered.toString() == "東京と",
           "live composing spans must not be mistaken for conversion segments");
}

}  // namespace

int main() {
    rendersUnicodeSpansAndByteCaretExactly();
    clampsMalformedCaretOffsetsWithoutChangingText();
    canAnchorTheInputPanelAtThePreeditStart();
    rendersDisplayOnlyBoundaryForTheActiveSegment();
    rendersEveryAdjacentConversionSegmentBoundary();
    doesNotRenderSegmentBoundariesWhileComposing();
    return 0;
}
