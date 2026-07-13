#include "hazkey_snapshot_renderer.h"

#include <algorithm>
#include <string_view>

#include <fcitx-utils/textformatflags.h>
#include <fcitx/inputpanel.h>

namespace fcitx {
namespace {
constexpr std::string_view kSegmentBoundary = "│";

TextFormatFlags formatFor(hazkey::PreeditSpan::Style style) {
    switch (style) {
        case hazkey::PreeditSpan::PLAIN:
            return TextFormatFlag::NoFlag;
        case hazkey::PreeditSpan::ACTIVE:
            return TextFormatFlag::HighLight;
        case hazkey::PreeditSpan::UNDERLINE:
        case hazkey::PreeditSpan::STYLE_UNSPECIFIED:
        default:
            return TextFormatFlag::Underline;
    }
}

TextFormatFlags segmentBoundaryFormat() {
    // The marker is display-only. Text::toStringForCommit() omits spans with
    // DontCommit, so no frontend path can include it in a committed string.
    return TextFormatFlags{TextFormatFlag::Bold} |
           TextFormatFlag::DontCommit;
}

bool needsSegmentBoundary(const hazkey::SessionSnapshot& snapshot, int index) {
    if (index <= 0 || index >= snapshot.preedit_size()) {
        return false;
    }
    switch (snapshot.phase()) {
        case hazkey::PREVIEWING:
        case hazkey::SELECTING:
        case hazkey::RECONVERTING:
            return true;
        case hazkey::IDLE:
        case hazkey::COMPOSING:
        case hazkey::UNICODE_INPUT:
        case hazkey::IME_PHASE_UNSPECIFIED:
        default:
            return false;
    }
}
}  // namespace

Text HazkeySnapshotRenderer::renderPreedit(
    const hazkey::SessionSnapshot& snapshot, bool cursorAtBeginning) {
    Text rendered;
    const auto caret = snapshot.has_caret_utf8_byte_offset()
        ? std::min<std::size_t>(snapshot.caret_utf8_byte_offset(),
                                utf8ByteLength(snapshot))
        : 0;
    std::size_t sourceOffset = 0;
    std::size_t renderedCaret = caret;
    for (int index = 0; index < snapshot.preedit_size(); ++index) {
        const auto& span = snapshot.preedit(index);
        if (needsSegmentBoundary(snapshot, index)) {
            rendered.append(std::string(kSegmentBoundary), segmentBoundaryFormat());
            // A caret exactly at the document boundary stays before the marker.
            // Carets in the following segment account for its display-only bytes.
            if (caret > sourceOffset) {
                renderedCaret += kSegmentBoundary.size();
            }
        }
        rendered.append(span.text(), formatFor(span.style()));
        sourceOffset += span.text().size();
    }
    if (snapshot.has_caret_utf8_byte_offset()) {
        // Fcitx Text's cursor is byte-based, matching the v2 snapshot unit.
        rendered.setCursor(cursorAtBeginning ? 0
                                             : static_cast<int>(renderedCaret));
    }
    return rendered;
}

void HazkeySnapshotRenderer::render(
    InputContext* inputContext, const hazkey::SessionSnapshot& snapshot,
    bool cursorAtBeginning) {
    if (inputContext == nullptr) {
        return;
    }
    auto rendered = renderPreedit(snapshot, cursorAtBeginning);
    if (inputContext->capabilityFlags().test(CapabilityFlag::Preedit)) {
        inputContext->inputPanel().setClientPreedit(rendered);
    } else {
        inputContext->inputPanel().setPreedit(rendered);
    }
    if (!snapshot.aux().empty()) {
        inputContext->inputPanel().setAuxDown(Text(snapshot.aux()));
    } else {
        inputContext->inputPanel().setAuxDown(Text());
    }
}

std::size_t HazkeySnapshotRenderer::utf8ByteLength(
    const hazkey::SessionSnapshot& snapshot) {
    std::size_t length = 0;
    for (const auto& span : snapshot.preedit()) {
        length += span.text().size();
    }
    return length;
}

}  // namespace fcitx
