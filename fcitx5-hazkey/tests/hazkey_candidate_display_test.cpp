#include <cstdlib>
#include <iostream>
#include <memory>
#include <string>

#include "hazkey_candidate_display.h"

namespace {

struct FakeCandidateList {
    int candidateCount = 0;

    int size() const { return candidateCount; }
};

[[noreturn]] void fail(const std::string& message) {
    std::cerr << message << '\n';
    std::exit(1);
}

void expect(bool condition, const std::string& message) {
    if (!condition) {
        fail(message);
    }
}

void disconnectedServerClearsCompositionWithoutUsingCandidates() {
    int readyCount = 0;
    int unavailableCount = 0;

    const bool displayed = fcitx::dispatchNonPredictCandidateList(
        false, std::shared_ptr<FakeCandidateList>{},
        [&readyCount](const auto&) { ++readyCount; },
        [&unavailableCount] { ++unavailableCount; });

    expect(!displayed, "a disconnected candidate request must not be displayed");
    expect(readyCount == 0, "a null candidate list must never be dereferenced");
    expect(unavailableCount == 1,
           "a disconnected request must clear local composition exactly once");
}

void emptyCandidateResponseClearsCompositionWithoutUsingCandidates() {
    int readyCount = 0;
    int unavailableCount = 0;
    auto emptyCandidates = std::make_shared<FakeCandidateList>();

    const bool displayed = fcitx::dispatchNonPredictCandidateList(
        true, emptyCandidates, [&readyCount](const auto&) { ++readyCount; },
        [&unavailableCount] { ++unavailableCount; });

    expect(!displayed, "an empty candidate response must not be displayed");
    expect(readyCount == 0, "an empty candidate list must never be focused");
    expect(unavailableCount == 1,
           "an empty response must clear local composition exactly once");
}

void validCandidateResponseUsesTheProductionReadyPath() {
    int readyCount = 0;
    int unavailableCount = 0;
    auto candidates = std::make_shared<FakeCandidateList>();
    candidates->candidateCount = 1;

    const bool displayed = fcitx::dispatchNonPredictCandidateList(
        true, candidates, [&readyCount](const auto&) { ++readyCount; },
        [&unavailableCount] { ++unavailableCount; });

    expect(displayed, "a non-empty candidate response must be displayed");
    expect(readyCount == 1, "the ready path must run exactly once");
    expect(unavailableCount == 0,
           "a valid response must preserve local composition");
}

}  // namespace

int main() {
    disconnectedServerClearsCompositionWithoutUsingCandidates();
    emptyCandidateResponseClearsCompositionWithoutUsingCandidates();
    validCandidateResponseUsesTheProductionReadyPath();
    return 0;
}
