#include <cstdlib>
#include <iostream>
#include <string>

#include "base.pb.h"
#include "hazkey_candidate.h"

namespace {

void expect(bool condition, const std::string& message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::exit(1);
    }
}

}  // namespace

int main() {
    google::protobuf::RepeatedPtrField<hazkey::CandidateSnapshot> candidates;
    auto* first = candidates.Add();
    first->set_id("generation-42-candidate-a");
    first->set_text("候補甲");
    auto* second = candidates.Add();
    second->set_id("generation-42-candidate-b");
    second->set_text("候補乙");

    std::string selectedID;
    uint64_t selectedGeneration = 0;
    fcitx::HazkeyCandidateList list(
        candidates, 42,
        [&](const std::string& id, uint64_t generation) {
            selectedID = id;
            selectedGeneration = generation;
        });

    list.candidate(1).select(nullptr);
    expect(selectedID == "generation-42-candidate-b",
           "mouse selection must preserve the authoritative candidate ID");
    expect(selectedGeneration == 42,
           "mouse selection must preserve the candidate generation");
    expect(list.size() == 2, "the renderer must expose every snapshot candidate");
    return 0;
}
