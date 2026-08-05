#!/usr/bin/env python3
"""Tic-Tac-Toe for the terminal: 2-player or vs an unbeatable AI."""

import random
import sys

WIN_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),  # rows
    (0, 3, 6), (1, 4, 7), (2, 5, 8),  # columns
    (0, 4, 8), (2, 4, 6),             # diagonals
]


def render(board):
    def cell(i):
        return board[i] if board[i] != " " else str(i + 1)

    rows = [
        f" {cell(0)} | {cell(1)} | {cell(2)} ",
        f" {cell(3)} | {cell(4)} | {cell(5)} ",
        f" {cell(6)} | {cell(7)} | {cell(8)} ",
    ]
    sep = "\n---+---+---\n"
    print()
    print(sep.join(rows))
    print()


def winner(board):
    for a, b, c in WIN_LINES:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    if " " not in board:
        return "draw"
    return None


def empty_cells(board):
    return [i for i in range(9) if board[i] == " "]


def minimax(board, player, ai_mark, human_mark):
    result = winner(board)
    if result == ai_mark:
        return 1, None
    if result == human_mark:
        return -1, None
    if result == "draw":
        return 0, None

    moves = empty_cells(board)
    best_move = moves[0]
    if player == ai_mark:
        best_score = -2
        for m in moves:
            board[m] = player
            score, _ = minimax(board, human_mark, ai_mark, human_mark)
            board[m] = " "
            if score > best_score:
                best_score, best_move = score, m
    else:
        best_score = 2
        for m in moves:
            board[m] = player
            score, _ = minimax(board, ai_mark, ai_mark, human_mark)
            board[m] = " "
            if score < best_score:
                best_score, best_move = score, m
    return best_score, best_move


def ai_move(board, ai_mark, human_mark):
    empties = empty_cells(board)
    if len(empties) == 9:
        return random.choice(empties)
    _, move = minimax(board, ai_mark, ai_mark, human_mark)
    return move


def human_move(board, mark):
    while True:
        raw = input(f"Player {mark}, pick a cell (1-9): ").strip()
        if not raw.isdigit():
            print("Enter a number from 1 to 9.")
            continue
        pos = int(raw) - 1
        if pos < 0 or pos > 8:
            print("Enter a number from 1 to 9.")
            continue
        if board[pos] != " ":
            print("That cell is taken.")
            continue
        return pos


def choose_mode():
    print("Tic-Tac-Toe")
    print("1) Two players")
    print("2) Vs computer (unbeatable)")
    while True:
        choice = input("Choose mode (1 or 2): ").strip()
        if choice in ("1", "2"):
            return choice
        print("Enter 1 or 2.")


def play():
    mode = choose_mode()
    board = [" "] * 9
    marks = ["X", "O"]

    ai_mark = None
    if mode == "2":
        first = input("Do you want to go first? (y/n): ").strip().lower()
        if first.startswith("n"):
            ai_mark, human_mark = "X", "O"
        else:
            ai_mark, human_mark = "O", "X"

    render(board)
    turn = 0
    while True:
        mark = marks[turn % 2]
        if mode == "2" and mark == ai_mark:
            print(f"Computer ({mark}) is thinking...")
            pos = ai_move(board, ai_mark, human_mark)
        else:
            pos = human_move(board, mark)

        board[pos] = mark
        render(board)

        result = winner(board)
        if result == "draw":
            print("It's a draw!")
            break
        if result:
            if mode == "2" and result == ai_mark:
                print("Computer wins!")
            else:
                print(f"Player {result} wins!")
            break

        turn += 1


def main():
    try:
        while True:
            play()
            again = input("Play again? (y/n): ").strip().lower()
            if not again.startswith("y"):
                print("Thanks for playing!")
                break
    except (KeyboardInterrupt, EOFError):
        print("\nBye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
