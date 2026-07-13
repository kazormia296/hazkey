#include "hazkey_snapshot_renderer.h"

#include <algorithm>

#include <fcitx-utils/textformatflags.h>
#include <fcitx/inputpanel.h>

namespace fcitx {
namespace {
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
}  // namespace

Text HazkeySnapshotRenderer::renderPreedit(
    const hazkey::SessionSnapshot& snapshot) {
    Text rendered;
    for (const auto& span : snapshot.preedit()) {
        rendered.append(span.text(), formatFor(span.style()));
    }
    if (snapshot.has_caret_utf8_byte_offset()) {
        const auto caret = std::min<std::size_t>(
            snapshot.caret_utf8_byte_offset(), rendered.toString().size());
        // Fcitx Text's cursor is byte-based, matching the v2 snapshot unit.
        rendered.setCursor(static_cast<int>(caret));
    }
    return rendered;
}

void HazkeySnapshotRenderer::render(
    InputContext* inputContext, const hazkey::SessionSnapshot& snapshot) {
    if (inputContext == nullptr) {
        return;
    }
    auto rendered = renderPreedit(snapshot);
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
