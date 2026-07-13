#include "hazkey_candidate.h"

#include <memory>
#include <utility>

namespace fcitx {

void HazkeyCandidateWord::select(InputContext* inputContext) const {
    FCITX_UNUSED(inputContext);
    if (selectHandler_) {
        selectHandler_(id_, generation_);
    }
}

HazkeyCandidateList::HazkeyCandidateList(
    const google::protobuf::RepeatedPtrField<
        hazkey::CandidateSnapshot>& candidates,
    uint64_t generation,
    SelectHandler selectHandler) {
    setSelectionKey(defaultSelectionKeys);
    for (const auto& candidate : candidates) {
        append(std::make_unique<HazkeyCandidateWord>(
            candidate, generation, selectHandler));
    }
}

CandidateLayoutHint HazkeyCandidateList::layoutHint() const {
    return CandidateLayoutHint::Vertical;
}

}  // namespace fcitx
