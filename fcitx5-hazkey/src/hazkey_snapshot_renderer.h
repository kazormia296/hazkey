#ifndef FCITX5_HAZKEY_HAZKEY_SNAPSHOT_RENDERER_H_
#define FCITX5_HAZKEY_HAZKEY_SNAPSHOT_RENDERER_H_

#include <cstddef>
#include <string>

#include <fcitx/inputcontext.h>
#include <fcitx/text.h>

#include "base.pb.h"

namespace fcitx {

class HazkeySnapshotRenderer {
   public:
    static Text renderPreedit(const hazkey::SessionSnapshot& snapshot,
                              bool cursorAtBeginning = false);
    static void render(InputContext* inputContext,
                       const hazkey::SessionSnapshot& snapshot,
                       bool cursorAtBeginning = false);
    static std::size_t utf8ByteLength(const hazkey::SessionSnapshot& snapshot);
};

}  // namespace fcitx

#endif  // FCITX5_HAZKEY_HAZKEY_SNAPSHOT_RENDERER_H_
