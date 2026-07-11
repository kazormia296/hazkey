#ifndef FCITX5_HAZKEY_HAZKEY_CANDIDATE_DISPLAY_H_
#define FCITX5_HAZKEY_HAZKEY_CANDIDATE_DISPLAY_H_

#include <memory>
#include <utility>

namespace fcitx {

template <typename CandidateList, typename ReadyHandler,
          typename UnavailableHandler>
bool dispatchNonPredictCandidateList(
    bool candidateRequestSucceeded,
    const std::shared_ptr<CandidateList>& candidateList,
    ReadyHandler&& readyHandler, UnavailableHandler&& unavailableHandler) {
    if (!candidateRequestSucceeded || candidateList == nullptr ||
        candidateList->size() == 0) {
        std::forward<UnavailableHandler>(unavailableHandler)();
        return false;
    }

    std::forward<ReadyHandler>(readyHandler)(candidateList);
    return true;
}

}  // namespace fcitx

#endif  // FCITX5_HAZKEY_HAZKEY_CANDIDATE_DISPLAY_H_
