#ifndef FCITX5_HAZKEY_HAZKEY_CANDIDATE_H_
#define FCITX5_HAZKEY_HAZKEY_CANDIDATE_H_

#include <fcitx/candidatelist.h>
#include <fcitx/inputcontext.h>
#include <fcitx/text.h>

#include <cstdint>
#include <functional>
#include <string>
#include <utility>

#include "base.pb.h"

namespace fcitx {

inline const KeyList defaultSelectionKeys = {
    Key{FcitxKey_1}, Key{FcitxKey_2}, Key{FcitxKey_3}, Key{FcitxKey_4},
    Key{FcitxKey_5}, Key{FcitxKey_6}, Key{FcitxKey_7}, Key{FcitxKey_8},
    Key{FcitxKey_9}, Key{FcitxKey_0},
};

class HazkeyCandidateWord : public CandidateWord {
   public:
    using SelectHandler = std::function<void(const std::string&, uint64_t)>;

    HazkeyCandidateWord(
        const hazkey::CandidateSnapshot& data,
        uint64_t generation, SelectHandler selectHandler)
        : CandidateWord(Text(data.text())),
          id_(data.id()),
          generation_(generation),
          selectHandler_(std::move(selectHandler)) {}

    void select(InputContext* inputContext) const override;

   private:
    std::string id_;
    uint64_t generation_;
    SelectHandler selectHandler_;
};

class HazkeyCandidateList : public CommonCandidateList {
   public:
    using SelectHandler = HazkeyCandidateWord::SelectHandler;

    HazkeyCandidateList(
        const google::protobuf::RepeatedPtrField<
            hazkey::CandidateSnapshot>& candidates,
        uint64_t generation,
        SelectHandler selectHandler);

    CandidateLayoutHint layoutHint() const override;
};

}  // namespace fcitx

#endif  // FCITX5_HAZKEY_HAZKEY_CANDIDATE_H_
