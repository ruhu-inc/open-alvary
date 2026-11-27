// enter-key.js — tells whether Enter in the chat box sends the message.
// Added by agentic-web. See README.md in this folder.
//
// People who type Japanese, Chinese or Korean use an input method (IME). It turns the
// letters they type into characters, and they press Enter to confirm them. That Enter
// must not send a half-written message.

/**
 * Decides whether a keydown in the chat box sends the message.
 *
 * Enter sends, unless an input method is still composing text.
 */
export class EnterKey {
  /**
   * The keyCode browsers give a keydown that an input method handles.
   */
  static IME_KEY_CODE = 229;

  /**
   * Tell whether a keydown sends the message.
   *
   * :param event: The keydown event.
   * :return: True for Enter, false for other keys and for Enter while an input method composes.
   */
  static sendsMessage(event) {
    // Safari can report the Enter that confirms the characters with isComposing false.
    // Its keyCode is still 229.
    return event.key === "Enter" && !event.isComposing && event.keyCode !== EnterKey.IME_KEY_CODE;
  }
}
