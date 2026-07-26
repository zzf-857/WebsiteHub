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

export function createAgentChatTransport(
  options: AgentChatTransportOptions,
): DefaultChatTransport<AgentUIMessage> {
  return new DefaultChatTransport<AgentUIMessage>({
    api: AGENT_CHAT_API,
    credentials: "include",
    prepareSendMessagesRequest: ({ messages }) => ({
      body: prepareAgentChatRequest({
        message: latestAgentUserText(messages),
        conversationId: options.resolveConversationId(),
        ...(options.resolveMetadata ? { metadata: options.resolveMetadata() } : {}),
      }),
    }),
  });
}
