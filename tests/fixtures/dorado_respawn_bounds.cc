#include "Game/Game.h"
#include "Map/Map.h"
#include "Skill/Skills.h"
#include <cassert>
#include <fstream>
class TestGame : public Game {
 public:
  TestGame() { map = new Map(4, 4, "map.txt"); }
  void init() override {}
  void setPVCcmd(const Operation&) override {}
  void getMapAndPlayer(int, const char*) override {}
  int winnerJudge() override { return -1; }
};
class Respawn : public Dead {
 public:
  Respawn(Unit* u) : Dead(u, Pos(0, 0)) {}
  using Dead::check;
};
int main() {
  std::ofstream output("map.txt");
  for (int i=0; i<16; ++i) output << "0 ";
  output.close();
  TestGame game;
  Unit unit(&game, "test", 0, 0, 0, 1, Pos(0,0));
  Respawn respawn(&unit);
  assert(!respawn.check(Pos(-1, 0)));
  assert(!respawn.check(Pos(0, -1)));
  assert(!respawn.check(Pos(4, 0)));
  assert(!respawn.check(Pos(0, 4)));
  assert(!respawn.check(Pos(-1000, -1000)));
  assert(respawn.check(Pos(0, 0)));
  assert(respawn.check(Pos(3, 3)));
  game.getMap()->setHeight(3, 3, 1);
  assert(!respawn.check(Pos(3, 3)));
}
