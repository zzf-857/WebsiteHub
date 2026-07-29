import { DefaultChatTransport } from "ai";

import { AGENT_CHAT_API } from "./agent-client.ts";
import {
  latestAgentUserText,
  prepareAgentChatRequest,
  type AgentUIMessage,
} from "./agent-contract.ts";

export type AgentChatTransportOptions = {
  /**
   * Read lazily on every send: the id only exists after the first turn, when
   * the backend announces it in the stream's start metadata.
   */
  resolveConversationId: () => string | null;
  /** Per-turn client hints, e.g. whether the user allowed web search. */
  resolveMetadata?: () => Record<string, unknown> | undefined;
};

export function resolveAgentTurnId(
  messages: readonly Pick<AgentUIMessage, "id" | "role">[],
  messageId: string | undefined,
): string {
  const latestUserMessage = [...messages].reverse().find((message) => message.role === "user");
  const requestedUserMessage = messageId
    ? messages.find((message) => message.id === messageId && message.role === "user")
    : undefined;
  const turnId = requestedUserMessage?.id ?? latestUserMessage?.id;
  if (!turnId) throw new Error("Agent 回合缺少稳定标识");
  return turnId;
}

export function createAgentChatTransport(
  options: AgentChatTransportOptions,
): DefaultChatTransport<AgentUIMessage> {
  return new DefaultChatTransport<AgentUIMessage>({
    api: AGENT_CHAT_API,
    credentials: "include",
    prepareSendMessagesRequest: ({ messages, messageId }) => {
      // AI SDK keeps the submitted user message id stable when the same HTTP
      // request is retried. Reusing it as turnId binds every network attempt to
      // one server receipt without inventing a second client-side lifecycle.
      const turnId = resolveAgentTurnId(messages, messageId);
      return {
        body: prepareAgentChatRequest({
          message: latestAgentUserText(messages),
          turnId,
          conversationId: options.resolveConversationId(),
          ...(options.resolveMetadata ? { metadata: options.resolveMetadata() } : {}),
        }),
      };
    },
  });
}
