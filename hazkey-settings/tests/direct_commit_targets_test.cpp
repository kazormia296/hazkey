#include <cstdlib>
#include <iostream>
#include <string>

#include "controllers/direct_commit_targets.h"

namespace {

void expect(bool condition, const std::string& message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::exit(1);
    }
}

}  // namespace

int main() {
    using hazkey::settings::hasPunctuationDirectCommitTarget;
    using hazkey::settings::withPunctuationDirectCommitEnabled;

    expect(withPunctuationDirectCommitEnabled(0x01, true) == 0x01,
           "saving an enabled partial punctuation mask must preserve it");
    expect(withPunctuationDirectCommitEnabled(0x10, false) == 0x10,
           "disabling punctuation must preserve unknown target bits");
    expect(withPunctuationDirectCommitEnabled(0x10, true) == 0x1F,
           "newly enabling punctuation must add only the documented bits");
    expect(withPunctuationDirectCommitEnabled(0x15, false) == 0x10,
           "disabling punctuation must clear only its four target bits");
    expect(hasPunctuationDirectCommitTarget(0x08),
           "every documented punctuation bit must enable the checkbox");
    expect(!hasPunctuationDirectCommitTarget(0x10),
           "future target bits must not enable the punctuation checkbox");
    return 0;
}
